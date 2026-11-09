import asyncio
import base64
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

LAB_ROOT = Path(__file__).resolve().parent.parent
KUBECONFIG = str(LAB_ROOT / ".private" / "kubeconfig")
CACHE_ROOT = LAB_ROOT / ".private" / "agent_cache"
AGENTS_DIR = LAB_ROOT / "agents"
NAMESPACE = "agent-lab"
DEFAULT_MODEL = os.getenv("OPENCODE_MODEL", "ollama/gpt-oss:20b-32k")

# Runtime telemetry & feed store per agent (backed by .private/agent_cache/<agent>/state.json so nothing is lost on hibernate or restart)
agent_runtime: Dict[str, dict] = {}
infra_state_cache: dict = {"last_op": None, "last_op_ms": None}


def _agent_cache_dir(name: str) -> Path:
    d = CACHE_ROOT / name
    (d / "workspace").mkdir(parents=True, exist_ok=True)
    return d


def _save_agent_cache(name: str):
    if name not in agent_runtime:
        return
    rt = agent_runtime[name]
    cdir = _agent_cache_dir(name)
    payload = {
        "name": name,
        "active_session_id": rt.get("active_session_id"),
        "cached_files": rt.get("cached_files", []),
        "cached_sessions": rt.get("cached_sessions", []),
        "cached_transcripts": rt.get("cached_transcripts", {}),
        "cached_db_kb": rt.get("cached_db_kb", 0),
        "pending_hitl": rt.get("pending_hitl"),
        "feed": rt.get("feed", []),
        "tokens_in": rt.get("tokens_in", 0),
        "tokens_out": rt.get("tokens_out", 0),
    }
    try:
        (cdir / "state.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def _ensure_agent_runtime(name: str, idle_timeout: int = 180, auto_hibernate: bool = True) -> dict:
    if name not in agent_runtime:
        cdir = _agent_cache_dir(name)
        saved = {}
        state_file = cdir / "state.json"
        if state_file.exists():
            try:
                saved = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                saved = {}
        agent_runtime[name] = {
            "name": name,
            "status": "idle",  # idle | working | waiting_hitl | hibernated
            "current_step": "",
            "last_activity_ts": time.time(),
            "auto_hibernate": auto_hibernate,
            "idle_timeout_sec": idle_timeout,
            "active_session_id": saved.get("active_session_id"),
            "cached_files": saved.get("cached_files", []),
            "cached_sessions": saved.get("cached_sessions", []),
            "cached_transcripts": saved.get("cached_transcripts", {}),
            "cached_db_kb": saved.get("cached_db_kb", 0),
            "pending_hitl": saved.get("pending_hitl"),
            "feed": saved.get("feed", []),
            "tokens_in": saved.get("tokens_in", 0),
            "tokens_out": saved.get("tokens_out", 0),
        }
    return agent_runtime[name]


def _append_feed(
    agent_name: str,
    kind: str,
    text: str,
    *,
    tool: str = "",
    output: str = "",
    session_id: Optional[str] = None,
    node: Optional[str] = None,
):
    rt = _ensure_agent_runtime(agent_name)
    rt["last_activity_ts"] = time.time()
    rt["feed"].append(
        {
            "id": uuid.uuid4().hex[:8],
            "ts": time.strftime("%H:%M:%S"),
            "kind": kind,  # user | assistant | reasoning | tool | infra | hitl | error
            "text": text,
            "tool": tool,
            "output": output,
            "session_id": session_id or rt.get("active_session_id"),
            "node": node,
        }
    )
    if len(rt["feed"]) > 300:
        rt["feed"] = rt["feed"][-300:]
    _save_agent_cache(agent_name)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle_ollama_proxy(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter):
    try:
        upstream_r, upstream_w = await asyncio.open_connection("127.0.0.1", 11434)
        await asyncio.gather(_pipe(client_r, upstream_w), _pipe(upstream_r, client_w))
    except Exception:
        try:
            client_w.close()
        except Exception:
            pass


def _env() -> dict:
    env = os.environ.copy()
    env["KUBECONFIG"] = KUBECONFIG
    return env


def _run_cmd(cmd: List[str], timeout: int = 30, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=_env(), input=input_text, capture_output=True, text=True, timeout=timeout)


def _safe_rel_path(name: str) -> str:
    norm = os.path.normpath(name.lstrip("/")).replace("\\", "/")
    if norm.startswith("..") or "/.." in norm or norm == ".":
        raise HTTPException(status_code=400, detail="Chemin de fichier invalide")
    return norm


def _sync_agent_pod_to_cache(agent_name: str, pod_name: str, *, include_file_contents: bool = False) -> Optional[dict]:
    """Probes /data/workspace (recursively) and /data/opencode.db from the running Pod and caches sessions, transcripts, and files locally."""
    script = """
    import { readdirSync, statSync, readFileSync, existsSync } from "fs";
    import { Database } from "bun:sqlite";
    const includeContents = process.env.SYNC_CONTENTS === "1";
    const files = [];
    const fileMap = {};
    function walk(dir, prefix = "") {
      for (const entry of readdirSync(dir)) {
        if (entry === "AGENTS.md" || entry === "node_modules" || entry.startsWith(".")) continue;
        const full = dir + "/" + entry;
        const rel = prefix ? (prefix + "/" + entry) : entry;
        const st = statSync(full);
        if (st.isDirectory()) {
          walk(full, rel);
        } else if (st.isFile()) {
          files.push({ name: rel, size: st.size, mtime: st.mtimeMs });
          if (includeContents && st.size <= 512 * 1024) {
            fileMap[rel] = readFileSync(full).toString("base64");
          }
        }
      }
    }
    if (existsSync("/data/workspace")) {
      walk("/data/workspace", "");
    }
    let sessions = [];
    let transcripts = {};
    let dbSize = 0;
    if (existsSync("/data/opencode.db")) {
      dbSize = Math.round(statSync("/data/opencode.db").size / 1024);
      try {
        const db = new Database("/data/opencode.db", { readonly: true });
        sessions = db.query(`
          SELECT s.id, s.title, s.directory, s.tokens_input, s.tokens_output, s.idle_outcome, s.time_created,
                 (SELECT count(*) FROM session_message m WHERE m.session_id = s.id) as msg_count,
                 (SELECT seq FROM event_sequence e WHERE e.aggregate_id = s.id) as event_seq
          FROM session_v2 s ORDER BY s.time_created DESC LIMIT 12
        `).all();
        if (true) {
          for (const s of sessions.slice(0, 6)) {
            const rawMsgs = db.query("SELECT seq, type, time_created, data FROM session_message WHERE session_id = ? ORDER BY seq ASC").all(s.id);
            const steps = [];
            for (const m of rawMsgs) {
              try {
                const d = JSON.parse(m.data);
                if (m.type === "user") {
                  steps.push({ seq: m.seq, role: "user", text: d.text || "" });
                } else if (m.type === "assistant" && Array.isArray(d.content)) {
                  for (const c of d.content) {
                    if (c.type === "reasoning" && c.text) {
                      steps.push({ seq: m.seq, role: "reasoning", text: c.text });
                    } else if (c.type === "text" && c.text) {
                      steps.push({ seq: m.seq, role: "assistant", text: c.text });
                    } else if (c.type === "tool") {
                      const st = c.state || {};
                      const out = Array.isArray(st.content) ? st.content.map(x => x.text || "").join("\\n") : (st.output || "");
                      steps.push({
                        seq: m.seq,
                        role: "tool",
                        tool: c.name,
                        input: JSON.stringify(st.input || {}),
                        output: out.slice(0, 1500)
                      });
                    }
                  }
                }
              } catch {}
            }
            transcripts[s.id] = steps;
          }
        }
      } catch {}
    }
    console.log(JSON.stringify({ files, fileMap, sessions, transcripts, dbSize }));
    """
    cmd = ["kubectl", "-n", NAMESPACE, "exec", pod_name, "--", "env", f"SYNC_CONTENTS={'1' if include_file_contents else '0'}", "bun", "-e", script]
    probe = _run_cmd(cmd, timeout=8)
    if probe.returncode != 0 or not probe.stdout.strip():
        return None
    try:
        parsed = json.loads(probe.stdout.strip().splitlines()[-1])
        rt = _ensure_agent_runtime(agent_name)
        rt["cached_files"] = parsed.get("files", [])
        rt["cached_sessions"] = parsed.get("sessions", [])
        rt["cached_db_kb"] = parsed.get("dbSize", 0)
        if parsed.get("transcripts"):
            rt["cached_transcripts"].update(parsed["transcripts"])
        if not rt["active_session_id"] and rt["cached_sessions"]:
            rt["active_session_id"] = rt["cached_sessions"][0]["id"]

        # Backfill feed from SQLite transcript ONLY once if feed is completely empty and agent is not actively streaming
        if not rt["feed"] and rt.get("status") != "working" and rt["active_session_id"] and rt["active_session_id"] in rt["cached_transcripts"]:
            sid = rt["active_session_id"]
            seen_ids = {item.get("id") for item in rt["feed"]}
            for st in rt["cached_transcripts"][sid]:
                role = st.get("role", "assistant")
                item_id = f"sql-{sid[-4:]}-{st.get('seq')}-{role[:2]}"
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                if role == "tool":
                    rt["feed"].append({
                        "id": item_id,
                        "ts": "SQLite",
                        "kind": "tool",
                        "tool": st.get("tool", ""),
                        "text": f"🔧 {st.get('tool', 'tool')} → {st.get('input', '')[:120]}",
                        "output": st.get("output", ""),
                        "session_id": sid,
                        "node": None,
                    })
                else:
                    rt["feed"].append({
                        "id": item_id,
                        "ts": "SQLite",
                        "kind": role,
                        "tool": "",
                        "text": st.get("text", ""),
                        "output": "",
                        "session_id": sid,
                        "node": None,
                    })

        if include_file_contents:
            wdir = _agent_cache_dir(agent_name) / "workspace"
            for fname, b64 in (parsed.get("fileMap") or {}).items():
                rel = _safe_rel_path(fname)
                dest = wdir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(base64.b64decode(b64))
        _save_agent_cache(agent_name)
        return parsed
    except Exception:
        return None


async def _scale_agent(agent_name: str, replicas: int, reason: str = "") -> int:
    t0 = time.time()
    rt = _ensure_agent_runtime(agent_name)
    if replicas == 0:
        # Snapshot files and SQLite sessions to host cache BEFORE hibernating so everything stays consultable at 0/1!
        pod = _get_agent_pod(agent_name)
        if pod and pod.get("ready"):
            _sync_agent_pod_to_cache(agent_name, pod["name"], include_file_contents=True)

        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "-n",
            NAMESPACE,
            "scale",
            f"deploy/{agent_name}",
            "--replicas=0",
            env=_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        for _ in range(30):
            if not _get_agent_pod(agent_name):
                break
            await asyncio.sleep(1)
        ms = int((time.time() - t0) * 1000)
        if rt["status"] != "waiting_hitl":
            rt["status"] = "hibernated"
        rt["current_step"] = ""
        msg = reason or f"💤 Agent mis en hibernation (replicas=0, 0% CPU/RAM) en {ms} ms (sessions SQLite & fichiers mis en cache pour consultation hors-ligne)."
        _append_feed(agent_name, "infra", msg)
        infra_state_cache["last_op"] = f"Hibernate {agent_name} (1 → 0)"
        infra_state_cache["last_op_ms"] = ms
        return ms
    else:
        rt["last_activity_ts"] = time.time()
        if rt["status"] == "hibernated":
            rt["status"] = "working"
            rt["current_step"] = "Réveil du Pod (0 → 1)..."
        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "-n",
            NAMESPACE,
            "scale",
            f"deploy/{agent_name}",
            "--replicas=1",
            env=_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        rollout = await asyncio.create_subprocess_exec(
            "kubectl",
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            f"deploy/{agent_name}",
            "--timeout=90s",
            env=_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, roll_err = await rollout.communicate()
        ms = int((time.time() - t0) * 1000)
        pod = _get_agent_pod(agent_name)
        if rollout.returncode != 0 or not pod or not pod.get("ready"):
            err_detail = roll_err.decode("utf-8", errors="replace").strip()
            ev_res = _run_cmd(["kubectl", "-n", NAMESPACE, "get", "events", "--sort-by=.lastTimestamp"], timeout=5)
            last_ev = "\n".join(ev_res.stdout.strip().splitlines()[-6:]) if ev_res.returncode == 0 else ""
            _append_feed(
                agent_name,
                "error",
                f"⚠️ Échec ou délai dépassé lors du réveil du Pod `{agent_name}` ({err_detail}). Vérifiez le PVC Longhorn.",
                output=last_ev,
            )
            raise RuntimeError(f"Pod {agent_name} non prêt après 90s: {err_detail}")

        node = pod["node"] if pod else "unknown"
        rt["last_activity_ts"] = time.time()
        if rt["status"] == "hibernated":
            rt["status"] = "idle"
        _sync_agent_pod_to_cache(agent_name, pod["name"], include_file_contents=True)
        msg = reason or f"⚡ Agent réveillé (0 → 1) sur le nœud {node} en {ms} ms (PVC Longhorn attaché)."
        _append_feed(agent_name, "infra", msg, node=node)
        infra_state_cache["last_op"] = f"Wake {agent_name} (0 → 1)"
        infra_state_cache["last_op_ms"] = ms
        return ms


async def _auto_hibernate_reconciler():
    """Background Idle Controller (KEDA cooldownPeriod equivalent for stateful Agent Deployments)."""
    while True:
        await asyncio.sleep(3)
        try:
            res = _run_cmd(["kubectl", "-n", NAMESPACE, "get", "deploy", "-o", "json"], timeout=6)
            if res.returncode != 0:
                continue
            items = json.loads(res.stdout).get("items", [])
            now = time.time()
            for dep in items:
                name = dep.get("metadata", {}).get("name", "")
                if not name:
                    continue
                ann = dep.get("metadata", {}).get("annotations", {}) or {}
                auto_hib = ann.get("agent.platform/auto-hibernate", "true").lower() == "true"
                idle_timeout = int(ann.get("agent.platform/idle-timeout", "180") or 180)
                rt = _ensure_agent_runtime(name, idle_timeout=idle_timeout, auto_hibernate=auto_hib)
                rt["auto_hibernate"] = auto_hib
                rt["idle_timeout_sec"] = idle_timeout

                replicas = dep.get("spec", {}).get("replicas", 0)
                ready_replicas = dep.get("status", {}).get("readyReplicas", 0) or 0
                if replicas == 0:
                    rt["last_activity_ts"] = now
                    if rt["status"] not in ("waiting_hitl", "working"):
                        rt["status"] = "hibernated"
                    continue

                if ready_replicas == 0 or rt["status"] == "working":
                    rt["last_activity_ts"] = now
                    continue

                if auto_hib and (now - rt["last_activity_ts"] >= idle_timeout):
                    await _scale_agent(
                        name,
                        0,
                        reason=f"💤 Auto-Hibernate (KEDA Cooldown {idle_timeout}s d'inactivité atteint) : snapshot local des fichiers/sessions SQLite puis scale-to-zero (1 → 0).",
                    )
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    server = None
    try:
        server = await asyncio.start_server(_handle_ollama_proxy, "192.168.124.1", 11434)
    except Exception:
        pass
    reconciler_task = asyncio.create_task(_auto_hibernate_reconciler())
    yield
    reconciler_task.cancel()
    if server:
        server.close()
        await server.wait_closed()


app = FastAPI(title="Console Plateforme Agentique K8s — OpenCode v2 + Longhorn", lifespan=lifespan)


def _get_vm_states() -> dict:
    vms = {}
    ram_gb = 0
    for vm in ("agentlab-server", "agentlab-worker"):
        res = _run_cmd(["virsh", "-c", "qemu:///system", "domstate", vm], timeout=5)
        st = res.stdout.strip() if res.returncode == 0 else "missing"
        vms[vm] = st
        if st == "running":
            ram_gb += 4
    return {"vms": vms, "host_ram_gb": ram_gb}


def _get_agent_pod(agent_name: str) -> Optional[dict]:
    res = _run_cmd(["kubectl", "-n", NAMESPACE, "get", "pods", "-l", f"app={agent_name}", "-o", "json"], timeout=6)
    if res.returncode != 0:
        return None
    items = json.loads(res.stdout).get("items", [])
    if not items:
        return None
    items.sort(key=lambda p: (0 if not p.get("metadata", {}).get("deletionTimestamp") else 1))
    pod = items[0]
    meta = pod.get("metadata", {})
    spec = pod.get("spec", {})
    status = pod.get("status", {})
    phase = status.get("phase", "Unknown")
    if meta.get("deletionTimestamp"):
        phase = "Terminating"
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
    return {
        "name": meta.get("name"),
        "node": spec.get("nodeName"),
        "ip": status.get("podIP"),
        "phase": phase,
        "ready": ready,
    }


@app.get("/api/state")
async def get_cluster_state():
    vm_info = _get_vm_states()
    if vm_info["vms"].get("agentlab-server") != "running":
        return {
            "vms": vm_info["vms"],
            "host_ram_gb": vm_info["host_ram_gb"],
            "agents": [],
            "last_op": infra_state_cache["last_op"],
            "last_op_ms": infra_state_cache["last_op_ms"],
        }

    dep_res = _run_cmd(["kubectl", "-n", NAMESPACE, "get", "deploy", "-o", "json"], timeout=6)
    pvc_res = _run_cmd(["kubectl", "-n", NAMESPACE, "get", "pvc", "-o", "json"], timeout=6)
    vol_res = _run_cmd(["kubectl", "-n", "longhorn-system", "get", "volumes.longhorn.io", "-o", "json"], timeout=6)

    pvc_map = {}
    if pvc_res.returncode == 0:
        for pvc in json.loads(pvc_res.stdout).get("items", []):
            pvc_name = pvc.get("metadata", {}).get("name", "")
            pvc_map[pvc_name] = {
                "volumeName": pvc.get("spec", {}).get("volumeName", ""),
                "size": pvc.get("spec", {}).get("resources", {}).get("requests", {}).get("storage", "3Gi"),
                "phase": pvc.get("status", {}).get("phase", "Bound"),
                "storageClass": pvc.get("spec", {}).get("storageClassName", ""),
            }

    lh_map = {}
    if vol_res.returncode == 0:
        for v in json.loads(vol_res.stdout).get("items", []):
            vname = v.get("metadata", {}).get("name", "")
            lh_map[vname] = {
                "name": vname,
                "state": v.get("status", {}).get("state", "unknown"),
                "robustness": v.get("status", {}).get("robustness", "unknown"),
                "node": v.get("status", {}).get("currentNodeID", ""),
            }

    agents_list = []
    now = time.time()
    if dep_res.returncode == 0:
        for dep in json.loads(dep_res.stdout).get("items", []):
            meta = dep.get("metadata", {})
            name = meta.get("name", "")
            ann = meta.get("annotations", {}) or {}
            spec = dep.get("spec", {})
            status = dep.get("status", {})

            replicas = spec.get("replicas", 0)
            ready_replicas = status.get("readyReplicas", 0)
            role = ann.get("agent.platform/role", "OpenCode Durable Agent")
            description = ann.get("agent.platform/description", "Agent OpenCode v2 avec état SQLite sur Longhorn RWOP")
            model = ann.get("agent.platform/model", DEFAULT_MODEL)
            auto_hib = ann.get("agent.platform/auto-hibernate", "true").lower() == "true"
            idle_timeout = int(ann.get("agent.platform/idle-timeout", "180") or 180)

            rt = _ensure_agent_runtime(name, idle_timeout=idle_timeout, auto_hibernate=auto_hib)
            rt["auto_hibernate"] = auto_hib
            rt["idle_timeout_sec"] = idle_timeout

            pod = _get_agent_pod(name)
            pvc_claim = f"{name}-storage"
            pvc_info = pvc_map.get(pvc_claim, {"volumeName": "", "size": ann.get("agent.platform/storage-size", "2Gi"), "phase": "Bound"})
            lh_vol = lh_map.get(pvc_info["volumeName"], {"name": pvc_info["volumeName"], "state": "detached", "robustness": "unknown", "node": ""})

            if pod and pod["ready"] and pod["phase"] == "Running":
                # Only probe pod via kubectl exec when not actively streaming or when cache is empty (every >= 6s)
                last_probe = rt.get("_last_probe_ts", 0.0)
                if rt["status"] != "working" and (now - last_probe >= 6.0 or not rt["cached_sessions"]):
                    rt["_last_probe_ts"] = now
                    _sync_agent_pod_to_cache(name, pod["name"], include_file_contents=False)
                if rt["status"] == "hibernated":
                    rt["status"] = "idle"
                    rt["last_activity_ts"] = now
            elif replicas == 0 and rt["status"] not in ("waiting_hitl", "working"):
                rt["status"] = "hibernated"

            idle_elapsed = int(max(0, now - rt["last_activity_ts"]))
            hibernate_in = max(0, idle_timeout - idle_elapsed) if (replicas > 0 and rt["status"] != "working" and auto_hib) else None

            # Check if an HTML preview file exists in workspace
            html_file = next((f["name"] for f in rt["cached_files"] if f["name"].endswith(".html")), None)

            agents_list.append(
                {
                    "name": name,
                    "role": role,
                    "description": description,
                    "model": model,
                    "replicas": replicas,
                    "ready_replicas": ready_replicas,
                    "status": rt["status"],
                    "current_step": rt["current_step"],
                    "pod": pod,
                    "pvc": {"claim": pvc_claim, **pvc_info},
                    "volume": lh_vol,
                    "auto_hibernate": auto_hib,
                    "idle_timeout_sec": idle_timeout,
                    "idle_elapsed_sec": idle_elapsed,
                    "hibernate_in_sec": hibernate_in,
                    "workspace_files": rt["cached_files"],
                    "preview_html": html_file,
                    "sqlite_sessions": rt["cached_sessions"],
                    "db_size_kb": rt["cached_db_kb"],
                    "active_session_id": rt["active_session_id"],
                    "pending_hitl": rt["pending_hitl"],
                    "feed": rt["feed"],
                    "tokens_in": rt["tokens_in"],
                    "tokens_out": rt["tokens_out"],
                }
            )

    return {
        "vms": vm_info["vms"],
        "host_ram_gb": vm_info["host_ram_gb"],
        "agents": agents_list,
        "last_op": infra_state_cache["last_op"],
        "last_op_ms": infra_state_cache["last_op_ms"],
    }


# ==============================================================================
# YAML AGENT DEFINITION & K8S / KEDA MANIFEST GENERATOR
# ==============================================================================


def _build_k8s_manifests_from_agent_yaml(raw_yaml: str) -> dict:
    doc = yaml.safe_load(raw_yaml)
    if not isinstance(doc, dict):
        raise ValueError("Le document YAML est invalide.")
    meta = doc.get("metadata", {}) or {}
    spec = doc.get("spec", {}) or {}

    name = str(meta.get("name", "")).strip().lower()
    if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", name):
        raise ValueError("metadata.name doit être un identifiant DNS Kubernetes valide (ex: opencode-dev).")

    role = meta.get("role", "OpenCode Durable Agent")
    description = meta.get("description", "Agent OpenCode v2 avec état SQLite sur Longhorn RWOP")
    model = spec.get("model", DEFAULT_MODEL)

    storage = spec.get("storage", {}) or {}
    storage_size = storage.get("size", "2Gi")
    raw_sc = storage.get("storageClassName", "agent-longhorn")
    # Auto-normalize legacy storageClassName
    storage_class = "agent-longhorn" if raw_sc in ("longhorn-agent-storage", "", None) else raw_sc
    access_mode = storage.get("accessMode", "ReadWriteOncePod")

    scaling = spec.get("scaling", {}) or {}
    auto_hibernate = bool(scaling.get("autoHibernate", True))
    idle_timeout = int(scaling.get("idleTimeoutSeconds", 180))
    initial_replicas = int(scaling.get("initialReplicas", 1))

    hitl = spec.get("hitl", {}) or {}
    gated_cmds = hitl.get("gatedCommands", []) or []

    system_prompt = spec.get("systemPrompt", "").strip() or (
        "# Agent Engineering Standards\n"
        "- Working directory: `/data/workspace` (persistent Longhorn RWOP volume).\n"
        "- Prefer using the `write` tool to write complete, self-contained files.\n"
    )

    permissions = [{"action": "*", "resource": "*", "effect": "allow"}, {"action": "skill", "resource": "*", "effect": "deny"}]
    for pat in gated_cmds:
        permissions.append({"action": "shell", "resource": str(pat), "effect": "ask"})

    opencode_config = {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "providers": {
            "ollama": {
                "name": "Ollama Local GPU (32k ctx)",
                "package": "@opencode/ai/providers/openai-compatible",
                "env": ["OLLAMA_API_KEY"],
                "settings": {"baseURL": "http://192.168.124.1:11434/v1", "apiKey": "ollama"},
                "models": {
                    "gpt-oss:20b": {
                        "id": "gpt-oss:20b-32k",
                        "name": "GPT-OSS 20B (32k Context)",
                        "limit": {"context": 32768, "output": 8192},
                    },
                    "gpt-oss:20b-32k": {
                        "name": "GPT-OSS 20B (32k Context)",
                        "limit": {"context": 32768, "output": 8192},
                    },
                    "qwen2.5:7b": {
                        "name": "Qwen 2.5 7B",
                        "limit": {"context": 32768, "output": 8192},
                    },
                },
            }
        },
        "permissions": permissions,
    }

    cm_name = "opencode-auditor-config" if name == "opencode-sre" else f"{name}-config"
    configmap_manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": cm_name, "namespace": NAMESPACE},
        "data": {
            "opencode.json": json.dumps(opencode_config, indent=2),
            "AGENTS.md": system_prompt + "\n",
        },
    }

    pvc_manifest = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": f"{name}-storage", "namespace": NAMESPACE},
        "spec": {
            "accessModes": [access_mode],
            "storageClassName": storage_class,
            "resources": {"requests": {"storage": storage_size}},
        },
    }

    deployment_manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {"app": name, "app.kubernetes.io/part-of": "opencode-agent-platform"},
            "annotations": {
                "agent.platform/role": role,
                "agent.platform/description": description,
                "agent.platform/model": model,
                "agent.platform/auto-hibernate": "true" if auto_hibernate else "false",
                "agent.platform/idle-timeout": str(idle_timeout),
                "agent.platform/storage-size": storage_size,
                "agent.platform/spec-yaml": raw_yaml,
            },
        },
        "spec": {
            "replicas": initial_replicas,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name, "app.kubernetes.io/part-of": "opencode-agent-platform"}},
                "spec": {
                    "terminationGracePeriodSeconds": 5,
                    "automountServiceAccountToken": False,
                    "securityContext": {"fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                    "initContainers": [
                        {
                            "name": "prepare-data",
                            "image": "busybox:1.36",
                            "securityContext": {"runAsUser": 0},
                            "command": [
                                "sh",
                                "-c",
                                (
                                    "mkdir -p /data/home /data/cache /data/state /data/config/opencode /data/workspace /data/bin\n"
                                    "printf '#!/bin/sh\\nexec env BUN_BE_BUN=1 /usr/local/bin/opencode \"$@\"\\n' > /data/bin/bun\n"
                                    "printf '#!/bin/sh\\nexec /bin/sh \"$@\"\\n' > /data/bin/bash\n"
                                    "chmod +x /data/bin/bun /data/bin/bash\n"
                                    "echo 'export PATH=\"/data/bin:$PATH\"' > /data/home/.profile\n"
                                    "chown -R 10001:10001 /data\n"
                                ),
                            ],
                            "volumeMounts": [{"name": "state", "mountPath": "/data"}],
                        }
                    ],
                    "containers": [
                        {
                            "name": "opencode",
                            "image": "ghcr.io/anomalyco/opencode:2.0.0",
                            "imagePullPolicy": "IfNotPresent",
                            "workingDir": "/data/workspace",
                            "command": ["sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done"],
                            "env": [
                                {"name": "PATH", "value": "/data/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
                                {"name": "HOME", "value": "/data/home"},
                                {"name": "XDG_DATA_HOME", "value": "/data/home/.local/share"},
                                {"name": "XDG_CACHE_HOME", "value": "/data/cache"},
                                {"name": "XDG_STATE_HOME", "value": "/data/state"},
                                {"name": "XDG_CONFIG_HOME", "value": "/data/config"},
                                {"name": "OPENCODE_DB", "value": "/data/opencode.db"},
                                {"name": "OLLAMA_API_KEY", "value": "ollama"},
                            ],
                            "securityContext": {
                                "runAsUser": 10001,
                                "runAsGroup": 10001,
                                "runAsNonRoot": True,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "250m", "memory": "512Mi"},
                                "limits": {"cpu": "2", "memory": "2Gi"},
                            },
                            "volumeMounts": [
                                {"name": "state", "mountPath": "/data"},
                                {"name": "config", "mountPath": "/data/config/opencode/opencode.json", "subPath": "opencode.json"},
                                {"name": "config", "mountPath": "/data/config/opencode/AGENTS.md", "subPath": "AGENTS.md"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "state", "persistentVolumeClaim": {"claimName": f"{name}-storage"}},
                        {"name": "config", "configMap": {"name": cm_name}},
                    ],
                },
            },
        },
    }

    keda_scaledobject = {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledObject",
        "metadata": {"name": f"{name}-idle-scaler", "namespace": NAMESPACE},
        "spec": {
            "scaleTargetRef": {"kind": "Deployment", "name": name},
            "minReplicaCount": 0,
            "maxReplicaCount": 1,
            "pollingInterval": 10,
            "cooldownPeriod": idle_timeout,
            "triggers": [
                {
                    "type": "metrics-api",
                    "metadata": {
                        "targetValue": "1",
                        "url": f"http://app-chapeau.agent-lab.svc:8000/api/agents/{name}/metrics",
                        "valueLocation": "active_tasks",
                    },
                }
            ],
        },
    }

    return {
        "name": name,
        "configmap": configmap_manifest,
        "pvc": pvc_manifest,
        "deployment": deployment_manifest,
        "keda_scaledobject": keda_scaledobject,
        "idle_timeout": idle_timeout,
        "auto_hibernate": auto_hibernate,
    }


@app.get("/api/agents/boilerplates")
async def get_agent_boilerplates():
    items = []
    if AGENTS_DIR.exists():
        for p in sorted(AGENTS_DIR.glob("*.yaml")):
            content = p.read_text(encoding="utf-8")
            doc = yaml.safe_load(content) or {}
            meta = doc.get("metadata", {})
            items.append(
                {
                    "id": p.stem,
                    "name": meta.get("name", p.stem),
                    "role": meta.get("role", p.stem),
                    "yaml": content,
                }
            )
    return {"boilerplates": items}


class ApplyYamlPayload(BaseModel):
    yaml_content: str


@app.post("/api/agents/apply")
async def apply_agent_yaml(payload: ApplyYamlPayload):
    try:
        bundle = _build_k8s_manifests_from_agent_yaml(payload.yaml_content)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    name = bundle["name"]
    # Check if deployment already existed before applying; if not, purge any leftover local cache so history starts clean
    dep_existed = _run_cmd(["kubectl", "-n", NAMESPACE, "get", "deploy", name]).returncode == 0
    if not dep_existed:
        shutil.rmtree(CACHE_ROOT / name, ignore_errors=True)
        agent_runtime.pop(name, None)

    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    (AGENTS_DIR / f"{name}.yaml").write_text(payload.yaml_content, encoding="utf-8")

    cm_yaml = yaml.safe_dump(bundle["configmap"], sort_keys=False)
    res_cm = _run_cmd(["kubectl", "apply", "-f", "-"], input_text=cm_yaml)
    if res_cm.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Erreur ConfigMap: {res_cm.stderr}")

    # Check if PVC exists; if it is stuck in Pending due to bad storageClassName, recreate it
    pvc_check = _run_cmd(["kubectl", "-n", NAMESPACE, "get", "pvc", f"{name}-storage", "-o", "json"])
    need_pvc_apply = True
    if pvc_check.returncode == 0:
        pvc_obj = json.loads(pvc_check.stdout)
        if pvc_obj.get("status", {}).get("phase") == "Bound":
            need_pvc_apply = False
        else:
            _run_cmd(["kubectl", "-n", NAMESPACE, "delete", "pvc", f"{name}-storage", "--ignore-not-found=true"])

    if need_pvc_apply:
        pvc_yaml = yaml.safe_dump(bundle["pvc"], sort_keys=False)
        res_pvc = _run_cmd(["kubectl", "apply", "-f", "-"], input_text=pvc_yaml)
        if res_pvc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Erreur PVC: {res_pvc.stderr}")

    dep_yaml = yaml.safe_dump(bundle["deployment"], sort_keys=False)
    res_dep = _run_cmd(["kubectl", "apply", "-f", "-"], input_text=dep_yaml)
    if res_dep.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Erreur Deployment: {res_dep.stderr}")

    rt = _ensure_agent_runtime(name, idle_timeout=bundle["idle_timeout"], auto_hibernate=bundle["auto_hibernate"])
    rt["last_activity_ts"] = time.time()
    _append_feed(
        name,
        "infra",
        f"📦 Agent '{name}' déployé/mis à jour depuis le manifeste YAML (PVC {name}-storage sur StorageClass agent-longhorn, Auto-Hibernate={bundle['auto_hibernate']}, Cooldown={bundle['idle_timeout']}s).",
    )
    return {
        "status": "applied",
        "agent": name,
        "keda_scaledobject_yaml": yaml.safe_dump(bundle["keda_scaledobject"], sort_keys=False),
    }


@app.get("/api/agents/{agent_name}/yaml")
async def get_agent_yaml(agent_name: str):
    file_path = AGENTS_DIR / f"{agent_name}.yaml"
    if file_path.exists():
        raw_yaml = file_path.read_text(encoding="utf-8")
    else:
        res = _run_cmd(["kubectl", "-n", NAMESPACE, "get", "deploy", agent_name, "-o", "json"])
        if res.returncode != 0:
            raise HTTPException(status_code=404, detail="Agent introuvable")
        dep = json.loads(res.stdout)
        ann = dep.get("metadata", {}).get("annotations", {}) or {}
        raw_yaml = ann.get("agent.platform/spec-yaml", "")
    bundle = _build_k8s_manifests_from_agent_yaml(raw_yaml) if raw_yaml else None
    k8s_full = ""
    if bundle:
        k8s_full = "\n---\n".join(
            [
                yaml.safe_dump(bundle["pvc"], sort_keys=False).strip(),
                yaml.safe_dump(bundle["configmap"], sort_keys=False).strip(),
                yaml.safe_dump(bundle["deployment"], sort_keys=False).strip(),
                yaml.safe_dump(bundle["keda_scaledobject"], sort_keys=False).strip(),
            ]
        ) + "\n"
    return {"name": agent_name, "agent_yaml": raw_yaml, "k8s_manifests_yaml": k8s_full}


@app.get("/api/agents/{agent_name}/metrics")
async def get_agent_keda_metrics(agent_name: str):
    rt = _ensure_agent_runtime(agent_name)
    active = 1 if rt["status"] == "working" else 0
    return {"agent": agent_name, "active_tasks": active, "status": rt["status"]}


@app.get("/api/docs")
async def get_platform_docs():
    article_path = LAB_ROOT.parent / "article-k8s-eaia-plateform.md"
    adr_path = LAB_ROOT / "docs" / "ADR.md"
    return {
        "article_markdown": article_path.read_text(encoding="utf-8") if article_path.exists() else "",
        "adr_markdown": adr_path.read_text(encoding="utf-8") if adr_path.exists() else "",
    }


class AutoHibernateConfigPayload(BaseModel):
    auto_hibernate: bool
    idle_timeout_sec: int


@app.post("/api/agents/{agent_name}/autoscaling")
async def update_agent_autoscaling(agent_name: str, body: AutoHibernateConfigPayload):
    rt = _ensure_agent_runtime(agent_name)
    rt["auto_hibernate"] = body.auto_hibernate
    rt["idle_timeout_sec"] = max(15, body.idle_timeout_sec)
    rt["last_activity_ts"] = time.time()
    _run_cmd(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "annotate",
            f"deploy/{agent_name}",
            f"agent.platform/auto-hibernate={'true' if body.auto_hibernate else 'false'}",
            f"agent.platform/idle-timeout={rt['idle_timeout_sec']}",
            "--overwrite",
        ]
    )
    _append_feed(
        agent_name,
        "infra",
        f"⚙️ Configuration Auto-Hibernate mise à jour : activé={body.auto_hibernate}, cooldown={rt['idle_timeout_sec']}s.",
    )
    return {"status": "updated"}


@app.delete("/api/agents/{agent_name}")
async def delete_agent(agent_name: str):
    if agent_name == "opencode-sre":
        raise HTTPException(status_code=400, detail="L'agent principal opencode-sre ne peut pas être supprimé.")
    _run_cmd(["kubectl", "-n", NAMESPACE, "delete", "deploy", agent_name, "--ignore-not-found=true"])
    _run_cmd(["kubectl", "-n", NAMESPACE, "delete", "configmap", f"{agent_name}-config", "--ignore-not-found=true"])
    _run_cmd(["kubectl", "-n", NAMESPACE, "delete", "pvc", f"{agent_name}-storage", "--ignore-not-found=true"])
    shutil.rmtree(CACHE_ROOT / agent_name, ignore_errors=True)
    agent_runtime.pop(agent_name, None)
    return {"status": "deleted", "agent": agent_name}


# ==============================================================================
# PER-AGENT INFRASTRUCTURE OPERATIONS (WAKE / HIBERNATE / MIGRATE / VM)
# ==============================================================================


@app.post("/api/agents/{agent_name}/wake")
async def api_agent_wake(agent_name: str):
    ms = await _scale_agent(agent_name, 1)
    return {"status": "awake", "duration_ms": ms, "pod": _get_agent_pod(agent_name)}


@app.post("/api/agents/{agent_name}/hibernate")
async def api_agent_hibernate(agent_name: str):
    ms = await _scale_agent(agent_name, 0)
    return {"status": "hibernated", "duration_ms": ms}


@app.post("/api/agents/{agent_name}/stop")
async def api_agent_stop_task(agent_name: str):
    """Stops/aborts an in-flight OpenCode task on the agent without deleting the agent."""
    rt = _ensure_agent_runtime(agent_name)
    proc = rt.get("active_proc")
    if proc and proc.returncode is None:
        try:
            proc.terminate()
        except Exception:
            pass
    pod = _get_agent_pod(agent_name)
    if pod and pod.get("ready"):
        _run_cmd(["kubectl", "-n", NAMESPACE, "exec", pod["name"], "--", "sh", "-c", "pkill -f 'opencode run' || true"], timeout=5)
    rt["status"] = "idle" if (pod and pod.get("ready")) else "hibernated"
    rt["current_step"] = ""
    rt["pending_hitl"] = None
    _append_feed(agent_name, "infra", "⏹️ Exécution en cours interrompue manuellement par l'opérateur.")
    return {"status": rt["status"], "agent": agent_name}


@app.post("/api/agents/{agent_name}/migrate")
async def api_agent_migrate(agent_name: str):
    pod = _get_agent_pod(agent_name)
    if not pod or not pod["ready"]:
        raise HTTPException(status_code=400, detail="L'agent doit être actif (Running) pour migrer de nœud.")
    old_node = pod["node"]
    t0 = time.time()
    _run_cmd(["kubectl", "cordon", old_node])
    try:
        _run_cmd(["kubectl", "-n", NAMESPACE, "delete", "pod", pod["name"], "--wait=true"], timeout=45)
        rollout = await asyncio.create_subprocess_exec(
            "kubectl",
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            f"deploy/{agent_name}",
            "--timeout=90s",
            env=_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await rollout.communicate()
    finally:
        _run_cmd(["kubectl", "uncordon", old_node])

    ms = int((time.time() - t0) * 1000)
    new_pod = _get_agent_pod(agent_name)
    new_node = new_pod["node"] if new_pod else "unknown"
    if new_pod and new_pod.get("ready"):
        _sync_agent_pod_to_cache(agent_name, new_pod["name"], include_file_contents=True)
    _append_feed(
        agent_name,
        "infra",
        f"🔀 Migration Live inter-nœuds réussie : {old_node} → {new_node} en {ms} ms (PVC Longhorn ré-attaché sans perte de session SQLite).",
        node=new_node,
    )
    infra_state_cache["last_op"] = f"Migrate {agent_name} ({old_node} → {new_node})"
    infra_state_cache["last_op_ms"] = ms
    return {"status": "migrated", "from_node": old_node, "to_node": new_node, "duration_ms": ms}


@app.post("/api/infra/vm/{target}/{action}")
async def api_vm_control(target: str, action: str):
    t0 = time.time()
    if target == "worker" and action == "stop":
        cmd = [str(LAB_ROOT / "pause.sh"), "--worker-only"]
        label = "Stop VM agentlab-worker (-4 Go RAM)"
    elif target == "worker" and action == "start":
        cmd = [str(LAB_ROOT / "resume.sh")]
        label = "Start VM agentlab-worker"
    elif target == "all" and action == "pause":
        cmd = [str(LAB_ROOT / "pause.sh")]
        label = "Pause Lab (2 VMs off, -8 Go RAM)"
    elif target == "all" and action == "resume":
        cmd = [str(LAB_ROOT / "resume.sh")]
        label = "Resume Lab (2 VMs on)"
    else:
        raise HTTPException(status_code=400, detail="Action VM invalide")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    ms = int((time.time() - t0) * 1000)
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=stderr.decode() or stdout.decode())
    infra_state_cache["last_op"] = label
    infra_state_cache["last_op_ms"] = ms
    return {"status": "ok", "duration_ms": ms, **_get_vm_states()}


# ==============================================================================
# WORKSPACE FILES, WEB PREVIEW, SESSION TRANSCRIPTS & K8S LOGS (WORKS EVEN AT 0/1)
# ==============================================================================


@app.get("/api/agents/{agent_name}/workspace/file")
async def get_workspace_file(agent_name: str, name: str):
    rel_name = _safe_rel_path(name)
    pod = _get_agent_pod(agent_name)
    if pod and pod["ready"]:
        res = _run_cmd(["kubectl", "-n", NAMESPACE, "exec", pod["name"], "--", "cat", f"/data/workspace/{rel_name}"])
        if res.returncode == 0:
            wdir = _agent_cache_dir(agent_name) / "workspace"
            dest = wdir / rel_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(res.stdout, encoding="utf-8")
            return {"name": rel_name, "content": res.stdout, "source": "live-pod"}

    # Fallback to offline host cache when Pod is hibernated!
    cached_path = _agent_cache_dir(agent_name) / "workspace" / rel_name
    if cached_path.exists():
        return {"name": rel_name, "content": cached_path.read_text(encoding="utf-8", errors="replace"), "source": "offline-cache"}

    raise HTTPException(status_code=404, detail="Fichier introuvable dans le Pod ou dans le cache hors-ligne.")


@app.get("/api/agents/{agent_name}/workspace/download")
async def download_workspace_file(agent_name: str, name: str):
    """Downloads a single artifact from the live Pod or from the offline host cache."""
    file_data = await get_workspace_file(agent_name, name)
    dl_name = os.path.basename(file_data["name"])
    mime, _ = mimetypes.guess_type(dl_name)
    return Response(
        content=file_data["content"].encode("utf-8"),
        media_type=mime or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
    )


@app.get("/api/agents/{agent_name}/workspace/download-zip")
async def download_workspace_zip(agent_name: str):
    """Downloads a ZIP archive containing all cached/live workspace files and session transcripts."""
    pod = _get_agent_pod(agent_name)
    if pod and pod.get("ready"):
        _sync_agent_pod_to_cache(agent_name, pod["name"], include_file_contents=True)

    cdir = _agent_cache_dir(agent_name)
    wdir = cdir / "workspace"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if wdir.exists():
            for f in sorted(wdir.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(wdir).as_posix()
                    zf.write(f, arcname=f"workspace/{rel}")
        state_file = cdir / "state.json"
        if state_file.exists():
            zf.write(state_file, arcname="session_state_and_transcripts.json")

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{agent_name}-artifacts.zip"'},
    )


@app.get("/preview/{agent_name}/{filename:path}")
async def preview_agent_web_file(agent_name: str, filename: str):
    """Serves generated HTML/CSS/JS files directly in the browser (works both when Pod is Running and when Hibernated!)."""
    rel_name = _safe_rel_path(filename or "index.html")
    pod = _get_agent_pod(agent_name)
    content_bytes = None
    if pod and pod["ready"]:
        res = subprocess.run(
            ["kubectl", "-n", NAMESPACE, "exec", pod["name"], "--", "cat", f"/data/workspace/{rel_name}"],
            env=_env(),
            capture_output=True,
            timeout=10,
        )
        if res.returncode == 0:
            content_bytes = res.stdout
            dest = _agent_cache_dir(agent_name) / "workspace" / rel_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content_bytes)

    if content_bytes is None:
        cached_path = _agent_cache_dir(agent_name) / "workspace" / rel_name
        if cached_path.exists():
            content_bytes = cached_path.read_bytes()

    if content_bytes is None:
        raise HTTPException(status_code=404, detail=f"Fichier {rel_name} introuvable pour l'agent {agent_name}")

    mime, _ = mimetypes.guess_type(rel_name)
    return Response(content=content_bytes, media_type=mime or "text/html")


@app.get("/api/agents/{agent_name}/sessions/{session_id}")
async def get_session_transcript(agent_name: str, session_id: str):
    """Returns the full step-by-step SQLite transcript (user, reasoning, tool calls, assistant) even when Pod is hibernated."""
    rt = _ensure_agent_runtime(agent_name)
    pod = _get_agent_pod(agent_name)
    if pod and pod["ready"]:
        _sync_agent_pod_to_cache(agent_name, pod["name"], include_file_contents=True)
    steps = rt.get("cached_transcripts", {}).get(session_id, [])
    return {"agent": agent_name, "session_id": session_id, "steps": steps}


@app.get("/api/agents/{agent_name}/logs")
async def get_agent_k8s_logs(agent_name: str):
    """Returns Kubernetes events and Pod/PVC diagnostics for the agent."""
    ev_res = _run_cmd(["kubectl", "-n", NAMESPACE, "get", "events", "--sort-by=.lastTimestamp"], timeout=6)
    lines = [ln for ln in ev_res.stdout.splitlines() if agent_name in ln or "HEAD" in ln] if ev_res.returncode == 0 else []
    pvc_res = _run_cmd(["kubectl", "-n", NAMESPACE, "describe", "pvc", f"{agent_name}-storage"], timeout=6)
    pod = _get_agent_pod(agent_name)
    pod_desc = ""
    if pod:
        pd_res = _run_cmd(["kubectl", "-n", NAMESPACE, "describe", "pod", pod["name"]], timeout=6)
        pod_desc = "\n".join(pd_res.stdout.splitlines()[-35:]) if pd_res.returncode == 0 else ""
    return {
        "agent": agent_name,
        "events": "\n".join(lines[-25:]) or "Aucun événement récent.",
        "pvc_describe": "\n".join(pvc_res.stdout.splitlines()[-20:]) if pvc_res.returncode == 0 else pvc_res.stderr,
        "pod_describe": pod_desc or "Pod actuellement en hibernation (replicas=0).",
    }


@app.post("/api/agents/{agent_name}/workspace/clean")
async def clean_workspace(agent_name: str):
    pod = _get_agent_pod(agent_name)
    if not pod or not pod["ready"]:
        await _scale_agent(agent_name, 1, reason="⚡ Réveil automatique de l'agent pour réinitialiser `/data/workspace`...")
        pod = _get_agent_pod(agent_name)
    _run_cmd(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "exec",
            pod["name"],
            "--",
            "sh",
            "-c",
            "rm -rf /data/workspace/* /data/opencode.db*",
        ]
    )
    rt = _ensure_agent_runtime(agent_name)
    rt["cached_files"] = []
    rt["cached_sessions"] = []
    rt["cached_transcripts"] = {}
    rt["cached_db_kb"] = 0
    rt["active_session_id"] = None
    rt["pending_hitl"] = None
    rt["feed"].clear()
    # Clean cached workspace files on disk
    wdir = _agent_cache_dir(agent_name) / "workspace"
    for f in wdir.glob("*"):
        if f.is_file():
            f.unlink()
    _append_feed(agent_name, "infra", "🗑️ Workspace `/data/workspace` et base SQLite `/data/opencode.db` réinitialisés.")
    return {"status": "cleaned"}


# ==============================================================================
# CONVERSATIONAL AGENT RUNNER & REAL ACTIONABLE HITL WORKFLOW
# ==============================================================================


class AgentMessagePayload(BaseModel):
    prompt: str
    mode: str = "auto"  # "auto" | "hitl_plan"
    execution_prompt: Optional[str] = None
    session_id: Optional[str] = None
    continue_session: bool = True
    hibernate_on_hitl: bool = True
    model: Optional[str] = None


async def _run_opencode_turn(
    agent_name: str,
    prompt: str,
    *,
    session_id: Optional[str],
    continue_session: bool,
    model: str,
    opencode_agent: str = "build",
) -> dict:
    rt = _ensure_agent_runtime(agent_name)
    rt["status"] = "working"
    rt["current_step"] = f"Vérification du Pod Kubernetes (mode {opencode_agent})..."
    rt["last_activity_ts"] = time.time()

    pod = _get_agent_pod(agent_name)
    if not pod or not pod["ready"]:
        rt["current_step"] = "Réveil du Pod (0 → 1) & attachement du PVC Longhorn..."
        await _scale_agent(
            agent_name,
            1,
            reason="⚡ Pod en hibernation (0/1) — Réveil automatique et attachement du volume Longhorn RWOP...",
        )
        pod = _get_agent_pod(agent_name)
        rt["status"] = "working"

    node = pod["node"] if pod else "unknown"
    rt["current_step"] = f"Démarrage OpenCode ({opencode_agent}) sur {node}..."
    cmd = [
        "kubectl",
        "-n",
        NAMESPACE,
        "exec",
        pod["name"],
        "--",
        "opencode",
        "run",
        "--standalone",
        "--auto",
        "--thinking",
        "--format",
        "json",
        "--agent",
        opencode_agent,
        "--model",
        model,
    ]
    if session_id:
        cmd.extend(["--session", session_id])
    elif continue_session and rt.get("active_session_id"):
        cmd.extend(["--session", rt["active_session_id"]])
    elif continue_session and rt.get("cached_sessions"):
        cmd.extend(["--continue"])
    else:
        cmd.extend(["--title", prompt[:48]])

    cmd.append(prompt)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    rt["active_proc"] = proc

    last_text = ""
    asked_question = None
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        rt["last_activity_ts"] = time.time()
        raw = line.decode("utf-8", errors="replace").strip()
        if not raw or not raw.startswith("{"):
            continue
        try:
            evt = json.loads(raw)
        except Exception:
            continue

        if evt.get("sessionID"):
            rt["active_session_id"] = evt["sessionID"]

        etype = evt.get("type")
        part = evt.get("part", {})

        if etype == "reasoning":
            r_txt = part.get("text", "").strip()
            if r_txt:
                rt["current_step"] = f"🧠 Réflexion LLM: {r_txt[:75]}..."
                _append_feed(agent_name, "reasoning", r_txt, session_id=rt["active_session_id"], node=node)
        elif etype == "text":
            txt = part.get("text", "").strip()
            if txt:
                last_text = txt
                rt["current_step"] = "✍️ Rédaction de la réponse..."
                _append_feed(agent_name, "assistant", txt, session_id=rt["active_session_id"], node=node)
        elif etype == "tool_use":
            tool_name = part.get("tool", "tool")
            state = part.get("state", {})
            inp = state.get("input", {})
            out = state.get("output", "")
            rt["current_step"] = f"🔧 Exécution outil: {tool_name}"
            if tool_name == "question":
                q_text = inp.get("question") or inp.get("text") or json.dumps(inp)
                asked_question = q_text
                _append_feed(
                    agent_name,
                    "hitl",
                    f"❓ L'agent demande une clarification humaine : {q_text}",
                    tool="question",
                    session_id=rt["active_session_id"],
                    node=node,
                )
            else:
                target_hint = inp.get("path") or inp.get("filePath") or inp.get("command") or json.dumps(inp)[:120]
                _append_feed(
                    agent_name,
                    "tool",
                    f"🔧 {tool_name} → {target_hint}",
                    tool=tool_name,
                    output=(out[:900] + ("\n..." if len(out) > 900 else "")) if out else "",
                    session_id=rt["active_session_id"],
                    node=node,
                )
        elif etype == "step_finish":
            tok = part.get("tokens", {})
            rt["tokens_in"] += tok.get("input", 0)
            rt["tokens_out"] += tok.get("output", 0)

    _, stderr_bytes = await proc.communicate()
    if proc.returncode != 0 and stderr_bytes:
        err_str = stderr_bytes.decode("utf-8", errors="replace").strip()
        if err_str:
            _append_feed(agent_name, "error", f"⚠️ Sortie d'erreur OpenCode (code={proc.returncode})", output=err_str, node=node)

    # Sync workspace files and SQLite session transcripts to host cache immediately!
    if pod and pod.get("name"):
        _sync_agent_pod_to_cache(agent_name, pod["name"], include_file_contents=True)

    return {
        "returncode": proc.returncode or 0,
        "last_text": last_text,
        "asked_question": asked_question,
        "node": node,
        "session_id": rt.get("active_session_id"),
    }


async def _orchestrate_agent_message(agent_name: str, payload: AgentMessagePayload):
    rt = _ensure_agent_runtime(agent_name)
    model = payload.model or DEFAULT_MODEL
    try:
        _append_feed(agent_name, "user", payload.prompt, session_id=payload.session_id or rt.get("active_session_id"))

        if payload.mode == "hitl_plan":
            analysis_instruction = (
                f"{payload.prompt}\n\n"
                "Read the existing files in `/data/workspace` and propose a concise 4-bullet implementation plan. "
                "Wait for human approval before implementation."
            )
            res = await _run_opencode_turn(
                agent_name,
                analysis_instruction,
                session_id=payload.session_id,
                continue_session=payload.continue_session,
                model=model,
                opencode_agent="plan",
            )
            plan_summary = res["asked_question"] or res["last_text"] or "Plan d'implémentation prêt. Confirmez-vous l'exécution du code et des tests ?"
            default_followup = payload.execution_prompt or (
                f"Approved! Now implement the plan for:\n{payload.prompt}\n\n"
                "CRITICAL RULES:\n"
                "1. Use ONLY the `write` tool to write complete files from start to finish in `/data/workspace` (do NOT use the `edit` tool).\n"
                "2. Verify your work before finishing."
            )
            rt["pending_hitl"] = {
                "id": f"hitl-{uuid.uuid4().hex[:6]}",
                "created_at": time.strftime("%H:%M:%S"),
                "session_id": res["session_id"],
                "node": res["node"],
                "question": plan_summary,
                "default_execution_prompt": default_followup,
                "model": model,
            }
            rt["status"] = "waiting_hitl"
            rt["current_step"] = "En attente de validation humaine (HITL)"
            _save_agent_cache(agent_name)
            if payload.hibernate_on_hitl:
                await _scale_agent(
                    agent_name,
                    0,
                    reason="💤 Suspension HITL Zero-Waste : Pod mis en hibernation (replicas=0, 0% CPU/RAM) pendant la réflexion de l'opérateur. La session SQLite est persistée sur Longhorn et mise en cache.",
                )
                rt["status"] = "waiting_hitl"
                _save_agent_cache(agent_name)
        else:
            res = await _run_opencode_turn(
                agent_name,
                payload.prompt,
                session_id=payload.session_id,
                continue_session=payload.continue_session,
                model=model,
                opencode_agent="build",
            )
            if res["asked_question"]:
                rt["pending_hitl"] = {
                    "id": f"hitl-{uuid.uuid4().hex[:6]}",
                    "created_at": time.strftime("%H:%M:%S"),
                    "session_id": res["session_id"],
                    "node": res["node"],
                    "question": res["asked_question"],
                    "default_execution_prompt": "Proceed with the recommended option and write the complete files in `/data/workspace` using `write`.",
                    "model": model,
                }
                rt["status"] = "waiting_hitl"
                rt["current_step"] = "En attente de réponse humaine (HITL)"
                _save_agent_cache(agent_name)
                if payload.hibernate_on_hitl:
                    await _scale_agent(
                        agent_name,
                        0,
                        reason="💤 Suspension HITL Zero-Waste : Pod hiberné (0/1) en attendant votre réponse.",
                    )
                    rt["status"] = "waiting_hitl"
                    _save_agent_cache(agent_name)
            else:
                rt["status"] = "idle"
                rt["current_step"] = ""
                rt["last_activity_ts"] = time.time()
                _save_agent_cache(agent_name)
    except Exception as exc:
        rt["status"] = "idle"
        rt["current_step"] = ""
        _append_feed(agent_name, "error", f"❌ Erreur d'exécution : {exc}")


@app.post("/api/agents/{agent_name}/message")
async def send_agent_message(agent_name: str, payload: AgentMessagePayload):
    rt = _ensure_agent_runtime(agent_name)
    if rt["status"] == "working":
        raise HTTPException(status_code=409, detail="Cet agent exécute déjà une tâche.")
    asyncio.create_task(_orchestrate_agent_message(agent_name, payload))
    return {"status": "dispatched", "agent": agent_name}


class HitlActionPayload(BaseModel):
    approved: bool
    feedback: str = ""
    migrate_to_peer_node: bool = False


async def _execute_hitl_resolution(agent_name: str, body: HitlActionPayload):
    rt = _ensure_agent_runtime(agent_name)
    hitl = rt.get("pending_hitl")
    if not hitl:
        return
    rt["pending_hitl"] = None
    _save_agent_cache(agent_name)

    if not body.approved:
        rt["status"] = "idle" if _get_agent_pod(agent_name) else "hibernated"
        rt["current_step"] = ""
        _append_feed(
            agent_name,
            "hitl",
            f"❌ Action HITL rejetée par l'opérateur : {body.feedback or 'Annulé'}",
            session_id=hitl.get("session_id"),
        )
        return

    rt["status"] = "working"
    rt["current_step"] = "Reprise post-HITL de la session SQLite..."
    _append_feed(
        agent_name,
        "hitl",
        f"✓ Validation HITL reçue ({body.feedback or 'Approuvé — Lancement de l’exécution'}). Reprise de la session SQLite `{hitl.get('session_id')}`...",
        session_id=hitl.get("session_id"),
    )

    prev_node = hitl.get("node")
    if body.migrate_to_peer_node and prev_node:
        _append_feed(
            agent_name,
            "infra",
            f"🔀 Option mobilité activée : Cordon du nœud initial `{prev_node}` pour forcer le réveil ou la bascule sur l'autre nœud K3s...",
        )
        _run_cmd(["kubectl", "cordon", prev_node])
        try:
            pod = _get_agent_pod(agent_name)
            if pod:
                _run_cmd(["kubectl", "-n", NAMESPACE, "delete", "pod", pod["name"], "--wait=true"], timeout=45)
            await _scale_agent(agent_name, 1, reason="⚡ Réveil post-HITL sur le nœud pair K3s avec rattachement du PVC Longhorn...")
        finally:
            _run_cmd(["kubectl", "uncordon", prev_node])
    else:
        pod = _get_agent_pod(agent_name)
        if not pod or not pod["ready"]:
            await _scale_agent(agent_name, 1, reason="⚡ Réveil automatique du Pod post-HITL (0 → 1)...")

    exec_prompt = hitl["default_execution_prompt"]
    if body.feedback.strip():
        exec_prompt = f"Operator feedback / instructions: {body.feedback.strip()}\n\n{exec_prompt}"

    await _run_opencode_turn(
        agent_name,
        exec_prompt,
        session_id=hitl.get("session_id"),
        continue_session=True,
        model=hitl.get("model", DEFAULT_MODEL),
        opencode_agent="build",
    )
    rt["status"] = "idle"
    rt["current_step"] = ""
    rt["last_activity_ts"] = time.time()
    _save_agent_cache(agent_name)


@app.post("/api/agents/{agent_name}/hitl")
async def resolve_agent_hitl(agent_name: str, body: HitlActionPayload):
    rt = _ensure_agent_runtime(agent_name)
    if not rt.get("pending_hitl"):
        raise HTTPException(status_code=404, detail="Aucun arbitrage HITL en attente pour cet agent.")
    asyncio.create_task(_execute_hitl_resolution(agent_name, body))
    return {"status": "resuming", "agent": agent_name}


# ==============================================================================
# HERDR-STYLE MULTI-AGENT WEB UI (VUE.JS 3)
# ==============================================================================


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    return r"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <title>Herdr K8s Console — OpenCode v2 + Longhorn + Auto-Hibernate</title>
  <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
  <style>
    :root {
      --bg: #0f172a;
      --sidebar: #1e293b;
      --surface: #ffffff;
      --surface-alt: #f8fafc;
      --border: #e2e8f0;
      --text: #0f172a;
      --muted: #64748b;
      --primary: #2563eb;
      --purple: #7c3aed;
      --green: #16a34a;
      --amber: #d97706;
      --red: #dc2626;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: var(--text);
      background: #f1f5f9;
      height: 100vh;
      overflow: hidden;
    }
    #app { display: flex; flex-direction: column; height: 100vh; }

    .topbar {
      background: #0f172a;
      color: #f8fafc;
      padding: 8px 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid #334155;
      font-size: 12.5px;
      flex-shrink: 0;
    }
    .topbar-group { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }

    .layout {
      display: grid;
      grid-template-columns: 330px 1fr;
      flex: 1;
      overflow: hidden;
    }

    .sidebar {
      background: #1e293b;
      color: #f8fafc;
      display: flex;
      flex-direction: column;
      border-right: 1px solid #334155;
      overflow: hidden;
    }
    .sidebar-header {
      padding: 14px 14px 10px;
      border-bottom: 1px solid #334155;
    }
    .sidebar-stats {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 6px;
      margin: 10px 0;
    }
    .stat-pill {
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 6px;
      padding: 6px 4px;
      text-align: center;
      font-size: 11px;
      cursor: pointer;
    }
    .stat-pill.active { border-color: #60a5fa; background: #1e3a8a; }
    .stat-pill strong { display: block; font-size: 14px; }
    .agent-list {
      flex: 1;
      overflow-y: auto;
      padding: 10px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .agent-card {
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 11px 12px;
      cursor: pointer;
      transition: all 0.15s;
    }
    .agent-card:hover { border-color: #64748b; }
    .agent-card.selected {
      background: #172554;
      border-color: #3b82f6;
      box-shadow: 0 0 0 1px #3b82f6;
    }
    .agent-card.hitl-alert { border-left: 4px solid #f59e0b; }
    .agent-card.working-alert { border-left: 4px solid #22c55e; }

    .main-pane {
      display: flex;
      flex-direction: column;
      overflow: hidden;
      background: #f1f5f9;
    }
    .agent-header {
      background: white;
      padding: 12px 18px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
    }
    .agent-meta-bar {
      background: #f8fafc;
      padding: 8px 18px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 12px;
      color: #334155;
      flex-wrap: wrap;
      gap: 10px;
    }
    .workspace-split {
      display: grid;
      grid-template-columns: 1fr 390px;
      flex: 1;
      overflow: hidden;
    }

    .feed-column {
      display: flex;
      flex-direction: column;
      overflow: hidden;
      border-right: 1px solid var(--border);
      background: #f8fafc;
    }
    .feed-scroll {
      flex: 1;
      overflow-y: auto;
      padding: 16px 18px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .feed-item {
      background: white;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 13px;
      font-size: 13px;
      line-height: 1.45;
    }
    .feed-user {
      background: #eff6ff;
      border-color: #bfdbfe;
      align-self: flex-end;
      max-width: 88%;
    }
    .feed-assistant {
      background: white;
      border-left: 3px solid #2563eb;
    }
    .feed-reasoning {
      background: #faf5ff;
      border-color: #e9d5ff;
      border-left: 3px solid #9333ea;
      color: #4c1d95;
      font-size: 12px;
    }
    .feed-tool {
      background: #0f172a;
      color: #e2e8f0;
      font-family: ui-monospace, monospace;
      font-size: 12px;
      padding: 8px 12px;
    }
    .feed-infra {
      background: #f1f5f9;
      border-style: dashed;
      color: #475569;
      font-size: 12px;
      padding: 7px 11px;
    }
    .feed-hitl {
      background: #fffbeb;
      border: 1px solid #fde68a;
      border-left: 4px solid #f59e0b;
    }
    .feed-error {
      background: #fef2f2;
      border-color: #fecaca;
      color: #991b1b;
    }

    .hitl-action-card {
      background: #fffbeb;
      border: 2px solid #f59e0b;
      border-radius: 8px;
      padding: 14px;
      margin: 8px 18px 0;
      box-shadow: 0 4px 12px rgba(245, 158, 11, 0.12);
    }

    .composer {
      background: white;
      border-top: 1px solid var(--border);
      padding: 12px 18px;
    }
    .preset-row {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }
    .preset-chip {
      background: #f1f5f9;
      color: #334155;
      border: 1px solid #cbd5e1;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 11.5px;
      cursor: pointer;
      font-weight: 500;
    }
    .preset-chip:hover { background: #e2e8f0; }

    .inspector-column {
      background: white;
      display: flex;
      flex-direction: column;
      overflow-y: auto;
      padding: 14px;
      gap: 14px;
    }
    .inspector-section {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      background: var(--surface-alt);
    }
    .inspector-section h4 {
      margin: 0 0 8px;
      font-size: 13px;
      color: #1e293b;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 10.5px;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 9999px;
      text-transform: uppercase;
    }
    .b-working { background: #dcfce7; color: #15803d; }
    .b-hitl { background: #fef3c7; color: #b45309; }
    .b-idle { background: #dbeafe; color: #1d4ed8; }
    .b-hibernated { background: #334155; color: #cbd5e1; }
    .b-red { background: #fee2e2; color: #b91c1c; }

    button, .btn-link {
      background: var(--primary);
      color: white;
      border: none;
      padding: 7px 12px;
      border-radius: 6px;
      cursor: pointer;
      font-weight: 600;
      font-size: 12px;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      transition: opacity 0.15s;
    }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    .btn-sm { padding: 4px 9px; font-size: 11.5px; }
    .btn-purple { background: var(--purple); }
    .btn-green { background: var(--green); }
    .btn-amber { background: var(--amber); }
    .btn-red { background: var(--red); }
    .btn-outline { background: white; color: #334155; border: 1px solid #cbd5e1; }
    .btn-dark { background: #334155; color: #f8fafc; border: 1px solid #475569; }

    input[type="text"], input[type="number"], textarea, select {
      width: 100%;
      padding: 7px 10px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      font-size: 12.5px;
      font-family: inherit;
    }
    pre.code-block {
      background: #0f172a;
      color: #f8fafc;
      padding: 10px;
      border-radius: 6px;
      font-size: 11.5px;
      overflow-x: auto;
      max-height: 280px;
      margin: 6px 0 0;
      white-space: pre-wrap;
    }

    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.72);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 50;
    }
    .modal-card {
      background: white;
      border-radius: 10px;
      width: 860px;
      max-width: 94vw;
      max-height: 90vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: 0 20px 40px rgba(0,0,0,0.3);
    }
    .modal-header {
      padding: 14px 18px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .modal-body {
      padding: 16px 18px;
      overflow-y: auto;
      flex: 1;
    }
    .yaml-editor {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12.5px;
      min-height: 360px;
      background: #0f172a;
      color: #e2e8f0;
      padding: 12px;
      border-radius: 6px;
      line-height: 1.45;
    }

    /* Interactive Lifecycle State Machine Bar (ADR-002) */
    .lifecycle-bar {
      background: #f8fafc;
      border-bottom: 1px solid var(--border);
      padding: 8px 20px;
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
    }
    .lc-step {
      border: 1px solid #cbd5e1;
      background: white;
      border-radius: 7px;
      padding: 7px 10px;
      font-size: 11px;
      color: #475569;
      transition: all 0.2s;
      position: relative;
    }
    .lc-step strong {
      display: block;
      font-size: 11.5px;
      color: #0f172a;
      margin-bottom: 3px;
    }
    .lc-step.active-hibernated {
      border: 2px solid #475569;
      background: #f1f5f9;
      box-shadow: 0 2px 8px rgba(15, 23, 42, 0.08);
    }
    .lc-step.active-working {
      border: 2px solid #16a34a;
      background: #f0fdf4;
      box-shadow: 0 2px 8px rgba(22, 163, 74, 0.15);
    }
    .lc-step.active-hitl {
      border: 2px solid #d97706;
      background: #fffbeb;
      box-shadow: 0 2px 8px rgba(217, 119, 6, 0.15);
    }
    .lc-step.active-idle {
      border: 2px solid #2563eb;
      background: #eff6ff;
      box-shadow: 0 2px 8px rgba(37, 99, 235, 0.15);
    }
    .doc-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      margin: 10px 0;
    }
    .doc-table th, .doc-table td {
      border: 1px solid #cbd5e1;
      padding: 7px 9px;
      text-align: left;
      vertical-align: top;
    }
    .doc-table th {
      background: #f1f5f9;
      font-weight: 700;
      color: #0f172a;
    }
    .doc-callout {
      background: #eff6ff;
      border-left: 4px solid #2563eb;
      padding: 10px 12px;
      border-radius: 4px;
      font-size: 12.5px;
      color: #1e3a8a;
      margin: 10px 0;
    }
  </style>
</head>
<body>
<div id="app">
  <!-- 1. TOP KVM INFRASTRUCTURE & HOST RAM BAR -->
  <div class="topbar">
    <div class="topbar-group">
      <strong style="font-size:13.5px;">🎛️ Enterprise AI Agent — Control Plane Kubernetes (OpenCode v2 · Longhorn RWOP · Scale 0↔1)</strong>
      <span style="color:#94a3b8;">|</span>
      <span>🖥️ <code>agentlab-server</code>:
        <span class="badge" :class="vms['agentlab-server'] === 'running' ? 'b-working' : 'b-hibernated'">{{ vms['agentlab-server'] || '...' }}</span>
      </span>
      <span>🖥️ <code>agentlab-worker</code>:
        <span class="badge" :class="vms['agentlab-worker'] === 'running' ? 'b-working' : 'b-hibernated'">{{ vms['agentlab-worker'] || '...' }}</span>
      </span>
      <span style="color:#cbd5e1;">RAM Cluster : <strong>{{ hostRamGb }} Go / 8 Go</strong></span>
    </div>
    <div class="topbar-group">
      <span v-if="lastOp" style="color:#38bdf8; font-size:11.5px;">Dernière op: {{ lastOp }} ({{ lastOpMs }} ms)</span>
      <button class="btn-purple btn-sm" @click="openDocModal('overview')" title="Architecture, cycle de vie en 7 étapes, API et article">
        📖 Architecture & Article
      </button>
      <button v-if="vms['agentlab-worker'] === 'running'" class="btn-dark btn-sm" @click="vmOp('worker', 'stop')" :disabled="busyInfra">⏹️ Stop Nœud Worker (-4 Go)</button>
      <button v-else class="btn-dark btn-sm" @click="vmOp('worker', 'start')" :disabled="busyInfra">▶️ Start Nœud Worker</button>
      <button v-if="vms['agentlab-server'] === 'running'" class="btn-amber btn-sm" @click="vmOp('all', 'pause')" :disabled="busyInfra">🌙 Pause Lab</button>
      <button v-else class="btn-green btn-sm" @click="vmOp('all', 'resume')" :disabled="busyInfra">☀️ Réveiller VMs</button>
    </div>
  </div>

  <!-- 2. MAIN HERDR-STYLE SPLIT VIEW -->
  <div class="layout">
    <!-- LEFT SIDEBAR: AGENT FEED & STATUS FILTER -->
    <aside class="sidebar">
      <div class="sidebar-header">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <strong style="font-size:14px; letter-spacing:0.3px;">📡 AGENTS DÉPLOYÉS ({{ agents.length }})</strong>
          <button class="btn-sm" @click="openYamlModal(null)">+ Patron YAML</button>
        </div>

        <div class="sidebar-stats">
          <div class="stat-pill" :class="{active: filterStatus === 'all'}" @click="filterStatus = 'all'">
            <strong>{{ agents.length }}</strong>Tous
          </div>
          <div class="stat-pill" :class="{active: filterStatus === 'working'}" @click="filterStatus = 'working'">
            <strong style="color:#4ade80;">{{ countByStatus('working') }}</strong>Work
          </div>
          <div class="stat-pill" :class="{active: filterStatus === 'waiting_hitl'}" @click="filterStatus = 'waiting_hitl'">
            <strong style="color:#fbbf24;">{{ countByStatus('waiting_hitl') }}</strong>HITL
          </div>
          <div class="stat-pill" :class="{active: filterStatus === 'hibernated'}" @click="filterStatus = 'hibernated'">
            <strong style="color:#94a3b8;">{{ countByStatus('hibernated') }}</strong>Sleep
          </div>
        </div>
      </div>

      <div class="agent-list">
        <div v-if="filteredAgents.length === 0" style="color:#94a3b8; font-size:12px; text-align:center; padding:24px 8px;">
          Aucun agent dans ce filtre. Cliquez sur <strong>+ Agent YAML</strong> pour déployer un agent.
        </div>

        <div
          v-for="ag in filteredAgents"
          :key="ag.name"
          class="agent-card"
          :class="{
            selected: selectedAgentName === ag.name,
            'hitl-alert': ag.status === 'waiting_hitl',
            'working-alert': ag.status === 'working'
          }"
          @click="selectedAgentName = ag.name"
        >
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
            <strong style="font-size:13.5px; color:#f8fafc;">{{ ag.name }}</strong>
            <span class="badge" :class="statusBadgeClass(ag)">
              {{ statusLabel(ag) }}
            </span>
          </div>
          <div style="font-size:11.5px; color:#94a3b8; margin-bottom:6px;">{{ ag.role }}</div>

          <div v-if="ag.status === 'working'" style="font-size:11.5px; color:#4ade80; background:rgba(22,163,74,0.15); padding:4px 7px; border-radius:4px; margin-bottom:6px;">
            ⚙️ {{ ag.current_step || 'Exécution en cours...' }}
          </div>
          <div v-if="ag.status === 'waiting_hitl'" style="font-size:11.5px; color:#fcd34d; background:rgba(217,119,6,0.2); padding:5px 7px; border-radius:4px; margin-bottom:6px;">
            ✋ <strong>Feedback HITL requis</strong> ({{ ag.replicas === 0 ? 'Pod hiberné 0% CPU' : 'Pod actif' }})
          </div>

          <div style="display:flex; justify-content:space-between; font-size:11px; color:#cbd5e1;">
            <span>☸️ {{ ag.pod ? (ag.pod.node || ag.pod.phase) : '0/1 (hiberné)' }}</span>
            <span v-if="ag.hibernate_in_sec !== null && ag.status === 'idle'" style="color:#38bdf8;">
              ⏱️ Sleep: {{ ag.hibernate_in_sec }}s
            </span>
            <span v-else>💾 {{ ag.sqlite_sessions.length }} sess. · {{ ag.workspace_files.length }} fich.</span>
          </div>
        </div>
      </div>
    </aside>

    <!-- RIGHT MAIN PANE: SELECTED AGENT WORKSPACE & CONVERSATION FEED -->
    <main class="main-pane" v-if="currentAgent">
      <div class="agent-header">
        <div>
          <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
            <h2 style="margin:0; font-size:18px;">{{ currentAgent.name }}</h2>
            <span class="badge" :class="statusBadgeClass(currentAgent)">{{ statusLabel(currentAgent) }}</span>
            <span style="font-size:12px; color:#64748b;">{{ currentAgent.role }}</span>
            <a
              v-if="currentAgent.preview_html"
              :href="`/preview/${currentAgent.name}/${currentAgent.preview_html}`"
              target="_blank"
              class="btn-link btn-green btn-sm"
            >
              🌐 Aperçu Site Web ({{ currentAgent.preview_html }}) ↗
            </a>
          </div>
          <div style="font-size:12px; color:#475569; margin-top:3px;">{{ currentAgent.description }}</div>
        </div>
        <div style="display:flex; gap:7px; flex-wrap:wrap;">
          <button class="btn-sm" @click="agentInfraOp('wake')" :disabled="busyInfra || (currentAgent.replicas > 0 && currentAgent.pod && currentAgent.pod.ready)">
            ⚡ Wake (0 → 1)
          </button>
          <button class="btn-outline btn-sm" @click="agentInfraOp('hibernate')" :disabled="busyInfra || currentAgent.replicas === 0">
            💤 Hibernate (1 → 0)
          </button>
          <button class="btn-purple btn-sm" @click="agentInfraOp('migrate')" :disabled="busyInfra || !currentAgent.pod || !currentAgent.pod.ready">
            🔀 Migrate Node (A → B)
          </button>
          <button class="btn-outline btn-sm" @click="openLogsModal">
            📜 Logs K8s / PVC
          </button>
          <button class="btn-outline btn-sm" @click="openDocModal('adr3')" title="Voir les appels API (apply, message, stop, delete) pour cet agent">
            🔌 API & Événements
          </button>
          <button class="btn-outline btn-sm" @click="openYamlModal(currentAgent.name)">
            📄 Patron YAML / KEDA
          </button>
          <button v-if="currentAgent.name !== 'opencode-sre'" class="btn-red btn-sm" @click="deleteCurrentAgent">
            🗑️
          </button>
        </div>
      </div>

      <div class="agent-meta-bar">
        <div style="display:flex; gap:14px; align-items:center; flex-wrap:wrap;">
          <span><strong>Pod :</strong> <code>{{ currentAgent.pod ? currentAgent.pod.name + ' (' + currentAgent.pod.phase + ')' : 'Hiberné (0 MiB RAM)' }}</code></span>
          <span><strong>Nœud K3s :</strong> <strong style="color:#1d4ed8;">{{ currentAgent.pod && currentAgent.pod.node ? currentAgent.pod.node : '—' }}</strong></span>
          <span><strong>PVC :</strong> <code>{{ currentAgent.pvc.claim }} ({{ currentAgent.pvc.phase }})</code>
            <span class="badge" :class="currentAgent.volume.state === 'attached' ? 'b-working' : 'b-hibernated'" style="margin-left:4px;">{{ currentAgent.volume.state }}</span>
          </span>
          <span><strong>SQLite <code>opencode.db</code> :</strong> {{ currentAgent.db_size_kb }} Ko ({{ currentAgent.sqlite_sessions.length }} sessions)</span>
        </div>

        <div style="display:flex; align-items:center; gap:8px;">
          <button class="btn-outline btn-sm" @click="showLifecycleBar = !showLifecycleBar" title="Afficher/Masquer le diagramme d'état interactif (ADR-002)">
            {{ showLifecycleBar ? '🔼 Masquer Cycle d’État' : '🔽 Afficher Cycle d’État (ADR-002)' }}
          </button>
          <label style="display:flex; align-items:center; gap:5px; cursor:pointer; font-weight:600;">
            <input type="checkbox" v-model="currentAgent.auto_hibernate" @change="saveAutoscaling">
            ⏱️ Auto-Hibernate
          </label>
          <select v-model.number="currentAgent.idle_timeout_sec" @change="saveAutoscaling" style="width:86px; padding:3px 6px; font-size:11.5px;">
            <option :value="60">60s</option>
            <option :value="120">120s</option>
            <option :value="180">180s</option>
            <option :value="300">300s</option>
          </select>
          <span v-if="currentAgent.hibernate_in_sec !== null && currentAgent.status === 'idle'" class="badge b-idle">
            Scale 1→0 dans {{ currentAgent.hibernate_in_sec }}s
          </span>
        </div>
      </div>

      <!-- PEDAGOGICAL LIVE STATE MACHINE BAR (ADR-002) -->
      <div v-if="showLifecycleBar" class="lifecycle-bar">
        <div class="lc-step" :class="{'active-hibernated': currentAgent.status === 'hibernated' || (currentAgent.replicas === 0 && currentAgent.status !== 'waiting_hitl')}">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <strong>1. 💤 HIBERNATED (0/1 · 0 Mio RAM)</strong>
            <span v-if="currentAgent.status === 'hibernated'" class="badge b-hibernated">ÉTAT ACTUEL</span>
          </div>
          <div>État initial au déploiement ou après inactivité. Pod éteint, PVC Longhorn <code>detached</code>. Consultation via le <strong>Snapshot Control Plane</strong>.</div>
        </div>

        <div class="lc-step" :class="{'active-working': currentAgent.status === 'working'}">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <strong>2. 🟢 WORKING (1/1 · Flux NDJSON)</strong>
            <span v-if="currentAgent.status === 'working'" class="badge b-working">ÉTAT ACTUEL</span>
          </div>
          <div>Réveil <code>0 → 1</code> sur événement API/Prompt, montage PVC <code>ReadWriteOncePod</code> sur <code>/data</code>, streaming <code>opencode run --format json</code>.</div>
        </div>

        <div class="lc-step" :class="{'active-hitl': currentAgent.status === 'waiting_hitl'}">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <strong>3. 🟡 WAITING HITL (0/1 · Pause Humaine)</strong>
            <span v-if="currentAgent.status === 'waiting_hitl'" class="badge b-hitl">ÉTAT ACTUEL</span>
          </div>
          <div>Fin Phase 1 (<code>--agent plan</code>) → <strong>Snapshot #1</strong> → Scale <code>1 → 0</code> (0% CPU) en attendant la validation humaine avant <code>--agent build</code>.</div>
        </div>

        <div class="lc-step" :class="{'active-idle': currentAgent.status === 'idle' && currentAgent.replicas > 0}">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <strong>4. 🔵 IDLE (1/1 · Snapshot #1 + Cooldown)</strong>
            <span v-if="currentAgent.status === 'idle' && currentAgent.replicas > 0" class="badge b-idle">ÉTAT ACTUEL</span>
          </div>
          <div>Fin de tâche → <strong>Snapshot #1 immédiat</strong> (workspace + SQLite disponibles même sans hiberner). Timer avant <strong>Snapshot final</strong> & Scale <code>1 → 0</code>.</div>
        </div>
      </div>

      <div class="workspace-split">
        <!-- LEFT: INTERACTIVE FEED & HITL -->
        <div class="feed-column">
          <div v-if="currentAgent.pending_hitl" class="hitl-action-card">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <strong>🛡️ Arbitrage Humain Requis (Session SQLite : <code>{{ currentAgent.pending_hitl.session_id || 'nouvelle' }}</code>)</strong>
              <span class="badge b-hitl">
                {{ currentAgent.replicas === 0 ? '💤 POD HIBERNÉ (0% CPU)' : '🟢 POD ACTIF SUR ' + (currentAgent.pod ? currentAgent.pod.node : '') }}
              </span>
            </div>
            <div style="background:white; border:1px solid #fde68a; border-radius:6px; padding:10px; font-size:12.5px; white-space:pre-wrap; max-height:180px; overflow-y:auto; margin-bottom:8px;">{{ currentAgent.pending_hitl.question }}</div>
            <input
              type="text"
              v-model="hitlReplyText"
              placeholder="Votre réponse ou ajustement pour la reprise (optionnel — par défaut : approuve et lance l'implémentation)..."
              style="margin-bottom:8px;"
            >
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
              <label style="display:flex; align-items:center; gap:6px; font-size:12px; cursor:pointer;">
                <input type="checkbox" v-model="hitlMigratePeer">
                <strong>🔀 Forcer le réveil sur l'autre nœud K3s</strong> (démontrer la reprise SQLite après bascule de VM)
              </label>
              <div style="display:flex; gap:8px;">
                <button class="btn-green" @click="submitHitl(true)">
                  ✓ Approuver & Exécuter l'implémentation
                </button>
                <button class="btn-red" @click="submitHitl(false)">
                  ✗ Rejeter
                </button>
              </div>
            </div>
          </div>

          <!-- LIVE WORKING BANNER -->
          <div v-if="currentAgent.status === 'working'" style="background:#dcfce7; border-bottom:1px solid #86efac; padding:8px 18px; font-size:12.5px; color:#166534; display:flex; justify-content:space-between; align-items:center;">
            <span>⚙️ <strong>Agent en cours de travail :</strong> {{ currentAgent.current_step || 'Exécution OpenCode...' }}</span>
            <div style="display:flex; gap:8px; align-items:center;">
              <span class="badge b-working">STREAMING NDJSON ACTIF</span>
              <button class="btn-red btn-sm" @click="agentInfraOp('stop')">⏹️ Stopper la tâche</button>
            </div>
          </div>

          <!-- FEED TIMELINE -->
          <div class="feed-scroll" ref="feedScroll">
            <div v-if="currentAgent.feed.length === 0" style="text-align:center; color:#64748b; padding:40px 20px; font-size:13px;">
              Aucun événement dans le flux de <strong>{{ currentAgent.name }}</strong>.<br>
              Utilisez un scénario rapide ci-dessous ou envoyez une instruction libre pour démarrer ou reprendre une session SQLite.
            </div>

            <div
              v-for="item in currentAgent.feed"
              :key="item.id"
              class="feed-item"
              :class="'feed-' + item.kind"
            >
              <div style="display:flex; justify-content:space-between; font-size:11px; opacity:0.75; margin-bottom:4px;">
                <span>
                  <strong v-if="item.kind === 'user'">👤 Opérateur</strong>
                  <strong v-else-if="item.kind === 'assistant'">🤖 {{ currentAgent.name }}</strong>
                  <strong v-else-if="item.kind === 'reasoning'">🧠 Réflexion LLM ({{ currentAgent.name }})</strong>
                  <strong v-else-if="item.kind === 'tool'">🔧 Outil OpenCode ({{ item.tool }})</strong>
                  <strong v-else-if="item.kind === 'hitl'">🛡️ Arbitrage HITL</strong>
                  <strong v-else-if="item.kind === 'infra'">☸️ Orchestrateur K8s / Longhorn</strong>
                  <strong v-else>⚠️ Diagnostic / Erreur</strong>
                  <span v-if="item.node"> · nœud <code>{{ item.node }}</code></span>
                  <span v-if="item.session_id"> · session <code>{{ item.session_id }}</code></span>
                </span>
                <span>{{ item.ts }}</span>
              </div>
              <div style="white-space:pre-wrap;">{{ item.text }}</div>
              <pre v-if="item.output" class="code-block">{{ item.output }}</pre>
            </div>
          </div>

          <!-- BOTTOM COMPOSER -->
          <div class="composer">
            <div class="preset-row">
              <span style="font-size:11.5px; color:#64748b; align-self:center; margin-right:4px;">Exemples :</span>
              <span class="preset-chip" @click="applyPreset(4)">🌐 Site Web DevOps (index.html + CSS moderne)</span>
              <span class="preset-chip" @click="applyPreset(1)">📦 Rate-Limiter TS + 5 tests (bun test)</span>
              <span class="preset-chip" @click="applyPreset(2)">🛡️ Penalty Box Circuit-Breaker (Mode HITL + Hibernate)</span>
            </div>

            <div style="display:flex; gap:10px; align-items:flex-start;">
              <textarea
                v-model="composerPrompt"
                placeholder="Écrivez une instruction pour l'agent (ex: 'Fais-moi un mini site web tech sur des sujets DevOps dans index.html')..."
                style="min-height:58px; flex:1;"
                @keydown.ctrl.enter="sendMessage"
              ></textarea>
              <div style="display:flex; flex-direction:column; gap:6px; width:235px;">
                <select v-model="composerMode" style="font-size:12px; padding:6px;">
                  <option value="auto">⚡ Mode Direct (Exécution)</option>
                  <option value="hitl_plan">🛡️ Mode HITL (Plan → Pause 0% CPU → Exécution)</option>
                </select>
                <button
                  :class="composerMode === 'hitl_plan' ? 'btn-purple' : ''"
                  @click="sendMessage"
                  :disabled="!composerPrompt.trim() || currentAgent.status === 'working'"
                >
                  {{ currentAgent.status === 'working' ? '⏳ Agent en travail...' : (composerMode === 'hitl_plan' ? '🛡️ Lancer avec HITL' : '🚀 Envoyer à l’agent') }}
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- RIGHT: WORKSPACE FILES (OFFLINE-CAPABLE) & SQLITE SESSION TRANSCRIPTS -->
        <div class="inspector-column">
          <div class="inspector-section">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
              <h4 style="margin:0;">📸 Snapshot Control Plane : <code>/data/workspace</code></h4>
              <div style="display:flex; gap:5px; flex-wrap:wrap;">
                <a
                  v-if="currentAgent.workspace_files.length > 0"
                  :href="`/api/agents/${currentAgent.name}/workspace/download-zip`"
                  class="btn-link btn-outline btn-sm"
                  title="Télécharger tous les artefacts et transcripts en .zip"
                >⬇️ .zip</a>
                <button class="btn-outline btn-sm" @click="cleanAgentWorkspace" :disabled="busyTest" title="Réinitialiser le workspace">🗑️</button>
              </div>
            </div>
            <div style="font-size:11px; color:#475569; margin-bottom:8px; background:#f8fafc; padding:6px 8px; border-radius:5px; border:1px solid #e2e8f0;">
              <div v-if="currentAgent.replicas > 0 && currentAgent.pod && currentAgent.pod.ready">
                🟢 <strong>Pod Actif (<code>1/1</code>) :</strong> Synchronisé à la transition <code>WORKING → IDLE</code> (<strong>Snapshot #1</strong>) + lecture directe sur le PVC Longhorn.
              </div>
              <div v-else>
                💤 <strong>Pod en Hibernation (<code>0/1</code>, PVC détaché) :</strong> Servi depuis le <strong>Snapshot Control Plane</strong> (disponible hors-ligne).
              </div>
            </div>
            <div v-if="currentAgent.workspace_files.length === 0" style="font-size:12px; color:#64748b;">
              Aucun fichier généré dans <code>/data/workspace</code>.
            </div>
            <div
              v-for="f in currentAgent.workspace_files"
              :key="f.name"
              style="display:flex; justify-content:space-between; align-items:center; padding:5px 0; border-bottom:1px dashed #e2e8f0; font-size:12.5px;"
            >
              <a href="#" @click.prevent="inspectFile(f.name)" style="color:#2563eb; font-weight:600; text-decoration:none;">📄 {{ f.name }}</a>
              <div style="display:flex; align-items:center; gap:5px;">
                <a v-if="f.name.endsWith('.html')" :href="`/preview/${currentAgent.name}/${f.name}`" target="_blank" style="font-size:11px; color:#16a34a; font-weight:700; text-decoration:none;" title="Prévisualiser dans le navigateur">🌐 Ouvrir ↗</a>
                <button class="btn-outline btn-sm" style="padding:2px 6px; font-size:10.5px;" @click="copyFileByName(f.name)" title="Copier le contenu dans le presse-papiers">
                  {{ copiedKey === f.name ? '✓ Copié' : '📋' }}
                </button>
                <a :href="`/api/agents/${currentAgent.name}/workspace/download?name=${encodeURIComponent(f.name)}`" class="btn-link btn-outline btn-sm" style="padding:2px 6px; font-size:10.5px;" title="Télécharger ce fichier">⬇️</a>
                <span style="color:#64748b; font-size:11px;">{{ f.size }} o</span>
              </div>
            </div>

            <div v-if="inspectedFile" style="margin-top:10px;">
              <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:4px;">
                <strong style="font-size:12px;"><code>{{ inspectedFile.name }}</code> <span class="badge b-idle">{{ inspectedFile.source }}</span></strong>
                <div style="display:flex; gap:5px;">
                  <button class="btn-outline btn-sm" @click="copyToClipboard(inspectedFile.content, 'viewer')">
                    {{ copiedKey === 'viewer' ? '✓ Copié !' : '📋 Copier' }}
                  </button>
                  <a :href="`/api/agents/${currentAgent.name}/workspace/download?name=${encodeURIComponent(inspectedFile.name)}`" class="btn-link btn-outline btn-sm">⬇️ Download</a>
                  <button class="btn-outline btn-sm" @click="inspectedFile = null">✕</button>
                </div>
              </div>
              <pre class="code-block">{{ inspectedFile.content }}</pre>
            </div>
          </div>

          <div class="inspector-section">
            <h4>🗄️ Sessions SQLite (<code>/data/opencode.db</code>)</h4>
            <div style="font-size:11px; color:#475569; margin-bottom:8px; background:#f8fafc; padding:6px 8px; border-radius:5px; border:1px solid #e2e8f0;">
              Base <strong>SQLite WAL</strong> sur bloc Longhorn <code>ReadWriteOncePod</code> (résiliente à la perte d'un nœud K3s). Synchronisée vers le Control Plane à chaque fin de tâche et avant hibernation.
              <a href="#" @click.prevent="openDocModal('adr1')" style="color:#2563eb; font-weight:600; text-decoration:none; display:inline-block; margin-top:2px;">
                📖 Pourquoi 1 Agent = 1 Pod + 1 PVC RWOP (ADR-001) →
              </a>
            </div>
            <div v-if="currentAgent.sqlite_sessions.length === 0" style="font-size:12px; color:#64748b;">
              Aucune session enregistrée dans SQLite.
            </div>
            <div
              v-for="s in currentAgent.sqlite_sessions"
              :key="s.id"
              @click="openSessionTranscript(s.id)"
              style="padding:7px 9px; border:1px solid #cbd5e1; border-radius:6px; background:white; margin-bottom:6px; font-size:11.5px; cursor:pointer;"
            >
              <div style="display:flex; justify-content:space-between;">
                <code style="color:#1d4ed8; font-weight:700;">🔍 {{ s.id }}</code>
                <span class="badge b-idle">{{ s.msg_count }} msgs · {{ s.event_seq || 0 }} evts</span>
              </div>
              <div style="color:#334155; margin-top:3px; font-weight:600;">{{ s.title }}</div>
              <div style="color:#64748b; margin-top:2px;">Tokens: {{ s.tokens_input }} in / {{ s.tokens_output }} out</div>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>

  <!-- 3. MODAL: SQLITE SESSION TRANSCRIPT OR K8S LOGS -->
  <div v-if="traceModalOpen" class="modal-backdrop" @click.self="traceModalOpen = false">
    <div class="modal-card">
      <div class="modal-header">
        <div>
          <h3 style="margin:0; font-size:16px;" v-if="traceModalMode === 'session'">
            🗄️ Transcript Complet Session SQLite <code>{{ selectedTranscriptSessionId }}</code> ({{ currentAgent.name }})
          </h3>
          <h3 style="margin:0; font-size:16px;" v-else>
            📜 Diagnostic Kubernetes & Événements Pod/PVC ({{ currentAgent.name }})
          </h3>
        </div>
        <button class="btn-outline btn-sm" @click="traceModalOpen = false">Fermer ✕</button>
      </div>
      <div class="modal-body">
        <div v-if="traceModalMode === 'session'">
          <div v-if="transcriptSteps.length === 0" style="color:#64748b; font-size:13px;">
            Aucune étape enregistrée pour cette session.
          </div>
          <div v-for="(st, idx) in transcriptSteps" :key="idx" class="feed-item" :class="'feed-' + st.role" style="margin-bottom:8px;">
            <div style="font-size:11px; font-weight:700; margin-bottom:4px; opacity:0.8;">
              [Seq #{{ st.seq }}] {{ st.role.toUpperCase() }} <span v-if="st.tool">({{ st.tool }})</span>
            </div>
            <div v-if="st.text" style="white-space:pre-wrap;">{{ st.text }}</div>
            <div v-if="st.input" style="font-size:11.5px; color:#93c5fd;">Input: <code>{{ st.input }}</code></div>
            <pre v-if="st.output" class="code-block">{{ st.output }}</pre>
          </div>
        </div>
        <div v-else>
          <h4>Événements Kubernetes récents (Namespace <code>agent-lab</code>)</h4>
          <pre class="code-block">{{ k8sLogs.events }}</pre>
          <h4 style="margin-top:12px;">État du PVC Longhorn (<code>{{ currentAgent.name }}-storage</code>)</h4>
          <pre class="code-block">{{ k8sLogs.pvc_describe }}</pre>
          <h4 style="margin-top:12px;">État du Pod</h4>
          <pre class="code-block">{{ k8sLogs.pod_describe }}</pre>
        </div>
      </div>
    </div>
  </div>

  <!-- 4. MODAL: CREATE / EDIT AGENT FROM YAML BOILERPLATE + KEDA SCALEDOBJECT -->
  <div v-if="yamlModalOpen" class="modal-backdrop" @click.self="yamlModalOpen = false">
    <div class="modal-card">
      <div class="modal-header">
        <div>
          <h3 style="margin:0; font-size:16px;">📄 Déclaration d'Agent par Manifeste YAML (<code>kind: DurableAgent</code>)</h3>
          <div style="font-size:12px; color:#64748b; margin-top:2px;">
            Provisionne automatiquement dans Kubernetes : <code>PersistentVolumeClaim</code> Longhorn RWOP (<code>agent-longhorn</code>) + <code>ConfigMap</code> OpenCode + <code>Deployment</code> + politique <code>Auto-Hibernate / KEDA</code>.
          </div>
        </div>
        <button class="btn-outline btn-sm" @click="yamlModalOpen = false">Fermer ✕</button>
      </div>
      <div class="modal-body">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:8px;">
          <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
            <span style="font-size:12px; font-weight:600;">Boilerplates YAML :</span>
            <button
              v-for="bp in boilerplates"
              :key="bp.id"
              class="btn-outline btn-sm"
              @click="yamlEditorContent = bp.yaml; yamlTab = 'agent'"
            >
              📋 {{ bp.name }} ({{ bp.role }})
            </button>
          </div>
          <div v-if="generatedK8sYaml" style="display:flex; gap:4px;">
            <button class="btn-sm" :class="yamlTab === 'agent' ? '' : 'btn-outline'" @click="yamlTab = 'agent'">1. DurableAgent YAML</button>
            <button class="btn-sm" :class="yamlTab === 'k8s' ? '' : 'btn-outline'" @click="yamlTab = 'k8s'">2. Manifestes K8s & KEDA générés</button>
          </div>
        </div>

        <textarea v-if="yamlTab === 'agent'" v-model="yamlEditorContent" class="yaml-editor"></textarea>
        <textarea v-else v-model="generatedK8sYaml" class="yaml-editor" readonly></textarea>

        <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:12px;">
          <button class="btn-outline" @click="yamlModalOpen = false">Annuler</button>
          <button class="btn-green" @click="applyAgentYaml" :disabled="busyYaml">
            {{ busyYaml ? '⏳ Application dans Kubernetes...' : '🚀 Appliquer & Déployer dans K3s (kubectl apply)' }}
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- 5. MODAL: ARCHITECTURE GUIDE, 3 ADRs, WEBHOOK TESTER & FULL TECHNICAL ARTICLE -->
  <div v-if="docModalOpen" class="modal-backdrop" @click.self="docModalOpen = false">
    <div class="modal-card" style="width:980px;">
      <div class="modal-header">
        <div>
          <h3 style="margin:0; font-size:16px;">📖 Guide d'Architecture, Décisions (ADRs) & Article Technique</h3>
          <div style="font-size:12px; color:#64748b; margin-top:2px;">
            Documentation interactive de la plateforme d'agents durables Kubernetes + OpenCode v2 + Longhorn.
          </div>
        </div>
        <button class="btn-outline btn-sm" @click="docModalOpen = false">Fermer ✕</button>
      </div>
      <div class="modal-body">
        <div style="display:flex; gap:6px; flex-wrap:wrap; margin-bottom:14px; border-bottom:1px solid #e2e8f0; padding-bottom:10px;">
          <button class="btn-sm" :class="docTab === 'overview' ? '' : 'btn-outline'" @click="docTab = 'overview'">📐 1. Architecture & 5 Briques</button>
          <button class="btn-sm" :class="docTab === 'adr2' ? '' : 'btn-outline'" @click="docTab = 'adr2'">🔄 2. États & Snapshots (ADR-002)</button>
          <button class="btn-sm" :class="docTab === 'adr1' ? '' : 'btn-outline'" @click="docTab = 'adr1'">🧱 3. Isolation 1 Agent = 1 Pod (ADR-001)</button>
          <button class="btn-sm" :class="docTab === 'adr3' ? '' : 'btn-outline'" @click="docTab = 'adr3'">⚡ 4. Réveil Événementiel & CronJob (ADR-003)</button>
          <button class="btn-sm" :class="docTab === 'article' ? 'btn-purple' : 'btn-outline'" @click="docTab = 'article'">📰 5. Article Complet (Markdown)</button>
        </div>

        <!-- TAB 1: OVERVIEW -->
        <div v-if="docTab === 'overview'" style="font-size:13px; line-height:1.55;">
          <div class="doc-callout">
            <strong>Pourquoi cette plateforme ? Du Harness Solo sur poste de travail à l'Enterprise AI Agent :</strong><br>
            Après le passage du <em>chatbot</em> à l'<em>agent</em> outillé sur poste individuel (OpenCode, Pi), l'étape suivante consiste à héberger plusieurs agents <strong>always-on</strong> (réveillés par alertes, webhooks ou tâches planifiées), partageables en équipe, gouvernés par validation humaine (HITL) et isolés.<br>
            Face aux plateformes intégrées du marché (<em>Buy</em> : ex. <em>Google Cloud Vertex AI Agent Engine</em>), ce laboratoire illustre l'approche modulaire sur Kubernetes (<em>Make</em>) et montre comment chaque choix technique (ex. OpenCode v2 avec état SQLite local) induit l'architecture des couches suivantes (stockage bloc distribué Longhorn <code>ReadWriteOncePod</code> puis Snapshot Control Plane).
          </div>
          <pre class="code-block" style="max-height:220px;">Chaîne de causalité technique :
[1. Besoin : Agent Harness]  ──► Choix : OpenCode v2 (Alternatives : Pi, Claude Code, Aider)
       │  (persiste ses sessions dans /data/opencode.db SQLite WAL et ses fichiers dans /data/workspace)
       ▼
[2. Besoin induit : Persistance & Mobilité Bloc]  ──► Choix : Longhorn CSI ReadWriteOncePod (Alternatives : Ceph RBD, EBS)
       │  (garantit 1 seul Pod écrivain et tolère la perte d'un nœud, mais démonte le disque quand Pod = 0/1)
       ▼
[3. Besoin : Scale-to-Zero (0 ↔ 1)]  ──► Choix : Cooldown Reconciler / KEDA ScaledObject
       │  (éteint le Pod après inactivité pour tomber à 0 Mio RAM, mais ne gère ni le réveil HTTP ni la lecture à froid)
       ▼
[4. Besoin induit : Agent Control Plane ("App Chapeau")]
          (porte l'API de réveil 0→1, l'état HITL --agent plan/build et le Snapshot sur transition d'état)</pre>

          <h4 style="margin:14px 0 6px;">Les 5 briques de l'architecture</h4>
          <table class="doc-table">
            <thead>
              <tr>
                <th>Brique</th>
                <th>Mécanisme technique</th>
                <th>Où l'observer dans cette console ?</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td><strong>1. Isolation (ADR-001)</strong></td>
                <td><code>1 Instance = 1 Pod + 1 PVC ReadWriteOncePod</code> (StorageClass <code>agent-longhorn</code>, <code>strategy: Recreate</code>).</td>
                <td>Bouton <strong>+ Agent YAML</strong> et barre de métadonnées (nom du Pod, nœud K3s et PVC dédié).</td>
              </tr>
              <tr>
                <td><strong>2. Scale-to-Zero (0 ↔ 1)</strong></td>
                <td>Tout agent démarre à <code>0/1</code>, se réveille à l'arrivée d'un prompt et retourne à <code>0/1</code> après <code>idleTimeoutSeconds</code>.</td>
                <td>Compteur <strong>Scale 1→0 dans Xs</strong> et boutons <strong>⚡ Wake / 💤 Hibernate</strong>.</td>
              </tr>
              <tr>
                <td><strong>3. Arbitrage Humain (HITL)</strong></td>
                <td>Phase 1 en lecture seule (<code>--agent plan</code>) → Hibernation <code>0/1</code> → Phase 2 sur la même session SQLite (<code>--agent build --session &lt;id&gt;</code>).</td>
                <td>Sélecteur <strong>🛡️ Mode HITL</strong> en bas du flux et carte d'approbation jaune avec option de migration de nœud.</td>
              </tr>
              <tr>
                <td><strong>4. Snapshots & Accès 0/1 (ADR-002)</strong></td>
                <td>Synchronisation de <code>/data/workspace</code> et <code>/data/opencode.db</code> dès la fin d'une tâche (<code>WORKING → IDLE</code>) et avant hibernation (<code>1 → 0</code>).</td>
                <td>Panneau de droite : aperçu web <code>🌐 Ouvrir</code>, <code>📋 Copier</code>, <code>⬇️ .zip</code> et sessions SQLite actifs même Pod éteint.</td>
              </tr>
              <tr>
                <td><strong>5. Réveil Événementiel (ADR-003)</strong></td>
                <td>Découplage entre le YAML de l'agent et son réveil par API HTTP (<code>POST /api/agents/{name}/message</code>) ou <code>CronJob</code> K8s.</td>
                <td>Onglet <strong>⚡ 4. Réveil Événementiel & CronJob</strong> de ce guide (avec bouton de simulation).</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- TAB 2: ADR-002 STATE MACHINE & SNAPSHOTS -->
        <div v-if="docTab === 'adr2'" style="font-size:13px; line-height:1.55;">
          <h4 style="margin-top:0;">ADR-002 : Pourquoi un Snapshot Control Plane en plus de Longhorn, et quand se déclenche-t-il ?</h4>
          <div class="doc-callout">
            <strong>Distinction fondamentale :</strong><br>
            1. <strong>Résilience aux pannes de nœud = Longhorn CSI (<code>numberOfReplicas: 2</code>)</strong> : réplique les blocs ext4 entre <code>agentlab-server</code> et <code>agentlab-worker</code>. Si une VM tombe, aucune donnée n'est perdue.<br>
            2. <strong>Consultation Pod éteint (<code>0/1</code>) = Snapshot Control Plane</strong> : quand le Pod hiberne (<code>replicas: 0</code>), son disque bloc ext4 est démonté (<code>detached</code>). Monter le PVC dans le Control Plane en <code>RWOP</code> bloquerait le réveil de l'agent, et passer en <code>RWX</code> (NFSv4) casserait SQLite WAL (<code>mmap</code>). Le Control Plane synchronise donc un miroir de lecture lors des changements d'état.
          </div>

          <table class="doc-table">
            <thead>
              <tr>
                <th>Transition / État du Pod</th>
                <th>État K8s & Longhorn</th>
                <th>Action déclenchée automatiquement</th>
                <th>Accès aux données dans l'App Chapeau</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td><strong>Déploiement initial → 💤 HIBERNATED</strong></td>
                <td><code>0/1</code> · PVC <code>detached</code></td>
                <td>Création du PVC, ConfigMap et Deployment à <code>replicas: 0</code>.</td>
                <td>0 Mio RAM consommés. Prêt à être réveillé.</td>
              </tr>
              <tr>
                <td><strong>💤 HIBERNATED → ⚡ WAKING → 🟢 WORKING</strong></td>
                <td><code>1/1 Running</code> · PVC <code>attached</code></td>
                <td><code>scale --replicas=1</code>, montage exclusif RWOP sur <code>/data</code>, lancement <code>opencode run --format json</code>.</td>
                <td>Streaming temps réel NDJSON des réflexions et appels d'outils.</td>
              </tr>
              <tr>
                <td><strong>🟢 WORKING → 🔵 IDLE (Tâche finie, Pod non hiberné)</strong></td>
                <td><code>1/1 Running</code> · PVC <code>attached</code></td>
                <td><strong>📸 SNAPSHOT #1 IMMÉDIAT</strong> (arborescence récursive <code>/data/workspace</code> + tables SQLite) + armement du timer Auto-Hibernate.</td>
                <td><strong>Immédiatement disponible</strong> (lecture directe Pod + miroir déjà synchronisé, même sans hibernation).</td>
              </tr>
              <tr>
                <td><strong>🟢 WORKING → 🟡 WAITING HITL</strong></td>
                <td><code>0/1</code> · PVC <code>detached</code></td>
                <td>Fin de <code>--agent plan</code> → <strong>📸 SNAPSHOT #1</strong> → <code>scale --replicas=0</code>.</td>
                <td>Plan et session SQLite consultables pendant que le Pod est à 0% CPU.</td>
              </tr>
              <tr>
                <td><strong>🔵 IDLE → 💤 HIBERNATED (Expiration Cooldown)</strong></td>
                <td><code>0/1</code> · PVC <code>detached</code></td>
                <td><strong>📸 SNAPSHOT FINAL PRÉ-ARRÊT (preStop)</strong> juste avant <code>scale --replicas=0</code>.</td>
                <td>Fichiers, aperçu HTML, export <code>.zip</code> et transcripts SQLite servis 100 % hors-ligne.</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- TAB 3: ADR-001 ISOLATION 1 AGENT = 1 POD -->
        <div v-if="docTab === 'adr1'" style="font-size:13px; line-height:1.55;">
          <h4 style="margin-top:0;">ADR-001 : Pourquoi 1 Instance d'Agent = 1 Pod dédié + 1 PVC Longhorn <code>ReadWriteOncePod</code> ?</h4>
          <p>
            Le manifeste YAML (<code>kind: DurableAgent</code>) représente un <strong>template d'agent</strong>. Chaque instance déployée reçoit son propre Pod et son propre PVC en mode <code>ReadWriteOncePod</code> (stable depuis Kubernetes v1.29) :
          </p>
          <ul>
            <li><strong>Intégrité SQLite WAL (<code>/data/opencode.db</code>) :</strong> SQLite WAL utilise un fichier de mémoire partagée (<code>opencode.db-shm</code> via <code>mmap</code>) qui exige un système de fichiers bloc local (<code>ext4</code>) avec un seul écrivain. <code>ReadWriteOncePod</code> garantit au niveau CSI qu'aucun second Pod ne peut monter le volume simultanément.</li>
            <li><strong>Isolation du Workspace (<code>/data/workspace</code>) :</strong> Évite toute collision de fichiers (<code>index.html</code>, <code>package.json</code>, index Git) entre deux agents travaillant en parallèle.</li>
            <li><strong>Cycle de vie <code>0 ↔ 1</code> indépendant :</strong> Chaque agent hiberne (0 Mio RAM) ou migre d'un nœud K3s à l'autre sans impacter les autres agents.</li>
          </ul>
          <h4>Comment instancier N fois un même template d'agent ?</h4>
          <p>
            <strong>1. Instances nommées par projet :</strong> Déployer plusieurs manifestes <code>DurableAgent</code> avec un <code>metadata.name</code> distinct (ex: <code>opencode-dev-website-blog</code>, <code>opencode-dev-website-doc</code>).<br>
            <strong>2. Pool indexé de workers identiques :</strong> Utiliser un <code>StatefulSet</code> Kubernetes muni d'un <code>volumeClaimTemplates</code> en <code>ReadWriteOncePod</code>, qui crée automatiquement les Pods isolés <code>agent-0</code>, <code>agent-1</code> et leurs PVCs dédiés <code>storage-agent-0</code>, <code>storage-agent-1</code>.
          </p>
        </div>

        <!-- TAB 4: ADR-003 EVENT API & CRONJOB -->
        <div v-if="docTab === 'adr3'" style="font-size:13px; line-height:1.55;">
          <h4 style="margin-top:0;">ADR-003 : Réveil sur Événement Externe (Webhook) ou Scheduler (<code>CronJob</code> K8s)</h4>
          <div class="doc-callout">
            <strong>Découplage Définition / Invocation :</strong> Un agent créé via son YAML démarre à l'état <code>HIBERNATED (0/1)</code>. Il ne contient ni démon cron interne ni serveur webhook (puisque son Pod est éteint). Tout réveil (<code>0 → 1</code> + exécution du prompt) passe par l'API événementielle du Control Plane : <code>POST /api/agents/{{ currentAgent ? currentAgent.name : 'opencode-sre' }}/message</code>.
          </div>

          <div style="background:#f8fafc; border:1px solid #cbd5e1; border-radius:8px; padding:12px; margin-bottom:12px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
              <strong>🚀 Tester un réveil événementiel en direct sur <code>{{ currentAgent ? currentAgent.name : 'opencode-sre' }}</code></strong>
              <button class="btn-green btn-sm" @click="simulateWebhookTrigger" :disabled="!currentAgent || currentAgent.status === 'working'">
                ⚡ Simuler un événement externe (Wake 0→1 + Prompt)
              </button>
            </div>
            <input type="text" v-model="webhookTestPrompt" style="margin-bottom:4px;">
          </div>

          <h4>1. Instancier un agent depuis son patron YAML (<code>POST /api/agents/apply</code>)</h4>
          <pre class="code-block">curl -X POST {{ baseUrl }}/api/agents/apply \
  -H "Content-Type: application/json" \
  -d "{\"yaml_content\": $(jq -Rs . &lt; agents/{{ currentAgent ? currentAgent.name : 'opencode-dev-website' }}.yaml)}"</pre>

          <h4 style="margin-top:12px;">2. Réveiller l'agent et envoyer un événement / prompt (<code>POST /api/agents/{name}/message</code>)</h4>
          <pre class="code-block">curl -X POST {{ baseUrl }}/api/agents/{{ currentAgent ? currentAgent.name : 'opencode-sre' }}/message \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "{{ webhookTestPrompt }}",
    "mode": "auto",
    "continue_session": true
  }'</pre>

          <h4 style="margin-top:12px;">3. Arrêter une tâche en cours ou supprimer l'agent (<code>POST .../stop</code> &amp; <code>DELETE</code>)</h4>
          <pre class="code-block"># Interrompre l'exécution OpenCode en cours :
curl -X POST {{ baseUrl }}/api/agents/{{ currentAgent ? currentAgent.name : 'opencode-dev-website' }}/stop

# Supprimer l'agent, son Deployment, son PVC Longhorn et son Snapshot :
curl -X DELETE {{ baseUrl }}/api/agents/{{ currentAgent ? currentAgent.name : 'opencode-dev-website' }}</pre>

          <h4 style="margin-top:12px;">4. Déclenchement périodique par un <code>CronJob</code> Kubernetes</h4>
          <pre class="code-block">apiVersion: batch/v1
kind: CronJob
metadata:
  name: trigger-{{ currentAgent ? currentAgent.name : 'opencode-sre' }}-morning
  namespace: agent-lab
spec:
  schedule: "0 8 * * 1-5"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: agent-trigger-caller # En prod : jeton projeté vérifié par TokenReview API
          restartPolicy: OnFailure
          containers:
            - name: webhook-caller
              image: curlimages/curl:8.10.1
              command:
                - sh
                - -c
                - |
                  curl -sf -X POST http://192.168.124.1:8000/api/agents/{{ currentAgent ? currentAgent.name : 'opencode-sre' }}/message \
                    -H "Content-Type: application/json" \
                    -d '{"prompt":"Exécute la revue quotidienne et mets à jour report.md","mode":"auto"}'</pre>
        </div>

        <!-- TAB 5: FULL ARTICLE MARKDOWN -->
        <div v-if="docTab === 'article'">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <span style="font-size:12px; color:#475569;">Fichier source : <code>k8s-opencode/ARTICLE_PLATEFORME_AGENTIQUE_K8S.md</code></span>
            <button class="btn-outline btn-sm" @click="copyToClipboard(docContent.article_markdown, 'article_md')">
              {{ copiedKey === 'article_md' ? '✓ Article copié !' : '📋 Copier le Markdown de l’article' }}
            </button>
          </div>
          <pre class="code-block" style="max-height:520px; font-size:12px; line-height:1.5;">{{ docContent.article_markdown }}</pre>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const { createApp } = Vue;
createApp({
  data() {
    return {
      vms: {},
      hostRamGb: 8,
      agents: [],
      selectedAgentName: 'opencode-dev-website',
      filterStatus: 'all',
      lastOp: null,
      lastOpMs: null,
      busyInfra: false,
      busyTest: false,
      composerPrompt: '',
      composerMode: 'auto',
      hitlReplyText: '',
      hitlMigratePeer: false,
      inspectedFile: null,
      copiedKey: '',
      yamlModalOpen: false,
      yamlTab: 'agent',
      yamlEditorContent: '',
      generatedK8sYaml: '',
      boilerplates: [],
      busyYaml: false,
      traceModalOpen: false,
      traceModalMode: 'session',
      selectedTranscriptSessionId: '',
      transcriptSteps: [],
      k8sLogs: { events: '', pvc_describe: '', pod_describe: '' },
      showLifecycleBar: true,
      docModalOpen: false,
      docTab: 'overview',
      docContent: { article_markdown: '', adr_markdown: '' },
      baseUrl: window.location.origin,
      webhookTestPrompt: 'Événement externe reçu : vérifie les fichiers présents dans /data/workspace et génère un fichier status-event.md résumant l état du projet.'
    };
  },
  computed: {
    filteredAgents() {
      if (this.filterStatus === 'all') return this.agents;
      return this.agents.filter(a => a.status === this.filterStatus);
    },
    currentAgent() {
      return this.agents.find(a => a.name === this.selectedAgentName) || this.agents[0] || null;
    }
  },
  methods: {
    countByStatus(st) {
      return this.agents.filter(a => a.status === st).length;
    },
    statusBadgeClass(ag) {
      if (ag.status === 'working') return 'b-working';
      if (ag.status === 'waiting_hitl') return 'b-hitl';
      if (ag.status === 'hibernated' || ag.replicas === 0) return 'b-hibernated';
      return 'b-idle';
    },
    statusLabel(ag) {
      if (ag.status === 'working') return '🟢 WORKING';
      if (ag.status === 'waiting_hitl') return '🟡 WAITING HITL';
      if (ag.status === 'hibernated' || ag.replicas === 0) return '💤 HIBERNATED (0/1)';
      return '🔵 RUNNING · IDLE';
    },
    async fetchState() {
      try {
        const r = await fetch('/api/state');
        if (!r.ok) return;
        const data = await r.json();
        this.vms = data.vms || {};
        this.hostRamGb = data.host_ram_gb ?? 8;
        this.agents = data.agents || [];
        this.lastOp = data.last_op;
        this.lastOpMs = data.last_op_ms;
        if (this.agents.length > 0 && !this.agents.some(a => a.name === this.selectedAgentName)) {
          this.selectedAgentName = this.agents[0].name;
        }
      } catch (e) {}
    },
    async vmOp(target, action) {
      this.busyInfra = true;
      try {
        await fetch(`/api/infra/vm/${target}/${action}`, { method: 'POST' });
        await this.fetchState();
      } finally {
        this.busyInfra = false;
      }
    },
    async agentInfraOp(op) {
      if (!this.currentAgent) return;
      this.busyInfra = true;
      try {
        await fetch(`/api/agents/${this.currentAgent.name}/${op}`, { method: 'POST' });
        await this.fetchState();
      } finally {
        this.busyInfra = false;
      }
    },
    async saveAutoscaling() {
      if (!this.currentAgent) return;
      await fetch(`/api/agents/${this.currentAgent.name}/autoscaling`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          auto_hibernate: this.currentAgent.auto_hibernate,
          idle_timeout_sec: this.currentAgent.idle_timeout_sec
        })
      });
      await this.fetchState();
    },
    applyPreset(num) {
      if (num === 4) {
        this.composerMode = 'auto';
        this.composerPrompt = `Crée un mini site web tech moderne sur les sujets DevOps & Kubernetes (GitOps, eBPF, Durable Agents K8s, CloudNativePG) dans /data/workspace en utilisant l'outil \`write\` :
1) Crée \`./index.html\` (avec du CSS moderne intégré ou \`./style.css\` et une interactivité Vue.js 3 ou JS vanilla pour filtrer les articles par tag DevOps/K8s/IA).
2) Vérifie que le fichier \`./index.html\` est bien présent et complet.`;
      } else if (num === 1) {
        this.composerMode = 'auto';
        this.composerPrompt = `Create two complete TypeScript files in /data/workspace using the \`write\` tool (./rate-limiter.ts and ./rate-limiter.test.ts):
1) rate-limiter.ts: Export interface TokenBucketOptions { capacity: number; refillRatePerSec: number }, interface AllowResult { allowed: boolean; remaining: number; retryAfterMs: number }, and class TokenBucketRateLimiter with methods:
   - allow(key: string, cost?: number, nowMs?: number): AllowResult
   - reset(key: string): void
   - pruneIdle(maxIdleMs: number, nowMs?: number): number
2) rate-limiter.test.ts: Import ONLY { TokenBucketRateLimiter } from "./rate-limiter" and { describe, test, expect } from "bun:test". Write 5 unit tests covering initial burst capacity, rejection with retryAfterMs, token refill over simulated nowMs, per-key isolation, and pruneIdle.
3) Run \`bun test\` via the \`shell\` tool and ensure all 5 tests pass with 0 errors.`;
      } else if (num === 2) {
        this.composerMode = 'hitl_plan';
        this.composerPrompt = `Extend TokenBucketRateLimiter in ./rate-limiter.ts and ./rate-limiter.test.ts with a Circuit-Breaker Penalty Box feature:
- Add optional constructor options \`maxConsecutiveRejections?: number\` and \`penaltyBoxMs?: number\`.
- When a key exceeds \`maxConsecutiveRejections\` consecutive rejected calls, block the key for \`penaltyBoxMs\` ms and expose \`isBlocked(key: string, nowMs?: number): boolean\`.
- Add unit tests in ./rate-limiter.test.ts and verify with \`bun test\`.`;
      }
    },
    async sendMessage() {
      if (!this.currentAgent || !this.composerPrompt.trim()) return;
      const prompt = this.composerPrompt.trim();
      this.composerPrompt = '';
      await fetch(`/api/agents/${this.currentAgent.name}/message`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          prompt,
          mode: this.composerMode,
          continue_session: true,
          hibernate_on_hitl: true,
          model: this.currentAgent.model
        })
      });
      await this.fetchState();
    },
    async submitHitl(approved) {
      if (!this.currentAgent) return;
      const feedback = this.hitlReplyText;
      this.hitlReplyText = '';
      await fetch(`/api/agents/${this.currentAgent.name}/hitl`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          approved,
          feedback,
          migrate_to_peer_node: this.hitlMigratePeer
        })
      });
      await this.fetchState();
    },
    async inspectFile(name) {
      if (!this.currentAgent) return;
      const r = await fetch(`/api/agents/${this.currentAgent.name}/workspace/file?name=` + encodeURIComponent(name));
      if (r.ok) {
        this.inspectedFile = await r.json();
      } else {
        const err = await r.json().catch(() => ({}));
        alert(err.detail || 'Impossible de lire le fichier.');
      }
    },
    async copyToClipboard(text, key) {
      try {
        await navigator.clipboard.writeText(text || '');
        this.copiedKey = key;
        setTimeout(() => { if (this.copiedKey === key) this.copiedKey = ''; }, 1800);
      } catch (e) {}
    },
    async copyFileByName(name) {
      if (!this.currentAgent) return;
      const r = await fetch(`/api/agents/${this.currentAgent.name}/workspace/file?name=` + encodeURIComponent(name));
      if (r.ok) {
        const d = await r.json();
        await this.copyToClipboard(d.content, name);
      }
    },
    async openSessionTranscript(sessionId) {
      if (!this.currentAgent) return;
      this.selectedTranscriptSessionId = sessionId;
      this.traceModalMode = 'session';
      this.traceModalOpen = true;
      const r = await fetch(`/api/agents/${this.currentAgent.name}/sessions/${encodeURIComponent(sessionId)}`);
      if (r.ok) {
        const d = await r.json();
        this.transcriptSteps = d.steps || [];
      }
    },
    async openLogsModal() {
      if (!this.currentAgent) return;
      this.traceModalMode = 'logs';
      this.traceModalOpen = true;
      const r = await fetch(`/api/agents/${this.currentAgent.name}/logs`);
      if (r.ok) {
        this.k8sLogs = await r.json();
      }
    },
    async openDocModal(tab = 'overview') {
      this.docTab = tab;
      this.docModalOpen = true;
      if (!this.docContent.article_markdown) {
        const r = await fetch('/api/docs');
        if (r.ok) {
          this.docContent = await r.json();
        }
      }
    },
    async simulateWebhookTrigger() {
      if (!this.currentAgent || !this.webhookTestPrompt.trim()) return;
      const prompt = this.webhookTestPrompt.trim();
      this.docModalOpen = false;
      await fetch(`/api/agents/${this.currentAgent.name}/message`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          prompt,
          mode: 'auto',
          continue_session: true,
          hibernate_on_hitl: true,
          model: this.currentAgent.model
        })
      });
      await this.fetchState();
    },
    async cleanAgentWorkspace() {
      if (!this.currentAgent) return;
      this.busyTest = true;
      try {
        await fetch(`/api/agents/${this.currentAgent.name}/workspace/clean`, { method: 'POST' });
        this.inspectedFile = null;
        await this.fetchState();
      } finally {
        this.busyTest = false;
      }
    },
    async openYamlModal(agentName) {
      const bpRes = await fetch('/api/agents/boilerplates');
      if (bpRes.ok) {
        const data = await bpRes.json();
        this.boilerplates = data.boilerplates || [];
      }
      this.yamlTab = 'agent';
      this.generatedK8sYaml = '';
      if (agentName) {
        const r = await fetch(`/api/agents/${agentName}/yaml`);
        if (r.ok) {
          const d = await r.json();
          this.yamlEditorContent = d.agent_yaml || (this.boilerplates[0] && this.boilerplates[0].yaml) || '';
          this.generatedK8sYaml = d.k8s_manifests_yaml || '';
        }
      } else {
        const devBp = this.boilerplates.find(b => b.id === 'opencode-dev') || this.boilerplates[0];
        this.yamlEditorContent = devBp ? devBp.yaml : '';
      }
      this.yamlModalOpen = true;
    },
    async applyAgentYaml() {
      this.busyYaml = true;
      try {
        const r = await fetch('/api/agents/apply', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ yaml_content: this.yamlEditorContent })
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          alert(err.detail || 'Erreur lors du déploiement YAML');
          return;
        }
        const res = await r.json();
        this.selectedAgentName = res.agent;
        this.yamlModalOpen = false;
        await this.fetchState();
      } finally {
        this.busyYaml = false;
      }
    },
    async deleteCurrentAgent() {
      if (!this.currentAgent || this.currentAgent.name === 'opencode-sre') return;
      if (!confirm(`Supprimer l'agent ${this.currentAgent.name} et son PVC Longhorn ?`)) return;
      await fetch(`/api/agents/${this.currentAgent.name}`, { method: 'DELETE' });
      this.selectedAgentName = 'opencode-sre';
      await this.fetchState();
    }
  },
  mounted() {
    this.fetchState();
    setInterval(this.fetchState, 2000);
  }
}).mount('#app');
</script>
</body>
</html>
"""


def _find_free_port(start_port: int = 8000) -> int:
    import socket

    port = start_port
    while port < 65535:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                port += 1
    raise RuntimeError(f"Aucun port libre trouvé à partir de {start_port}")


if __name__ == "__main__":
    import uvicorn

    preferred = int(os.getenv("PORT", "8000"))
    chosen_port = _find_free_port(preferred)
    if chosen_port != preferred:
        print(f"==> Port {preferred} déjà occupé -> bascule automatique sur le port libre {chosen_port}")
    print(f"==> Démarrage de l'App Chapeau sur http://localhost:{chosen_port}")
    uvicorn.run("app_chapeau.main:app", host="0.0.0.0", port=chosen_port, reload=True, app_dir=str(LAB_ROOT))

