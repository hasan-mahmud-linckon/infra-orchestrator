"""
Python Remote Infra Orchestrator
--------------------------------
- Reads a YAML/JSON config of servers and services
- Runs health checks (http or remote_cmd)
- Restarts unhealthy services
- Logs to file + console
- Sends notifications to Discord (via environment variable)

Usage:
    export DISCORD_WEBHOOK="https://discord.com/api/webhooks/xxxx/yyyy"
    python orchestrator.py --config config.yaml
"""

import argparse
import logging
import os
import re
import sys
import yaml
import json
import requests
import paramiko
from pathlib import Path
from logging.handlers import RotatingFileHandler

# ---------------------- Config Loader ----------------------

def load_config(path):
    p = Path(path)
    with open(p, "r") as f:
        if p.suffix in [".yaml", ".yml"]:
            return yaml.safe_load(f)
        elif p.suffix == ".json":
            return json.load(f)
        else:
            raise ValueError("Unsupported config format. Use .yaml, .yml or .json")


# ---------------------- Logging Setup ----------------------
def setup_logging(log_file:str):
    os.makedirs(Path(log_file).parent, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s",
                            "%Y-%m-%d %H:%M:%S")

    fh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)


# ---------------------- SSH Helper ----------------------
def run_ssh(server: dict, command: str):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    client.connect(
        hostname=server["host"],
        port=server.get("port", 22),
        username=server.get("username"),
        key_filename=server.get("private_key"),
        timeout=10
    )

    stdin, stdout, stderr = client.exec_command(command, timeout=10)
    exit_code = stdout.channel.recv_exit_status()
    out, err = stdout.read().decode().strip(), stderr.read().decode().strip()
    client.close()
    return exit_code, out, err



# ---------------------- Health Checks ----------------------
def health_check(server: dict):
    hc = server["health_check"]  
    hc_type = hc["type"]  

    if hc_type == "http":  
        try:  
            r = requests.get(hc["url"], timeout=hc.get("timeout", 5))  
            ok = r.status_code == hc.get("expected_status", 200)  
            if "contains" in hc:  
                ok = ok and hc["contains"] in r.text  
            return ok, f"HTTP {r.status_code}"  
        except Exception as e:  
            return False, f"HTTP error: {e}"  

    if hc_type == "remote_cmd":  
        code, out, err = run_ssh(server, hc["command"])  
        ok = code == 0  
        if "expect_stdout_regex" in hc:  
            ok = ok and re.search(hc["expect_stdout_regex"], out)  
        return ok, f"exit={code}, out={out}, err={err}"  

    return False, "Unknown health_check type"


# ---------------------- Service Restart ----------------------
def restart_service(server: dict):
    service = server.get("service")
    restart_cmd = server.get("restart_command", f"sudo systemctl restart {service}")
    return run_ssh(server, restart_cmd)


# ---------------------- Discord Notification ----------------------
def notify_discord(message: str):
    webhook = os.getenv("DISCORD_WEBHOOK")
    if not webhook:
        logging.error("DISCORD_WEBHOOK not set. Skipping notification.")
        return

    try:
        resp = requests.post(webhook, json={"content": message}, timeout=5)
        resp.raise_for_status()
        logging.info("Notification sent to Discord.")
    except Exception as e:
        logging.error(f"Discord notification failed: {e}")


# ---------------------- Main Logic ----------------------
def run_orchestrator(config_file: str):
    cfg = load_config(config_file)
    log_file = cfg.get("log_file", "logs/orchestrator.log")
    setup_logging(log_file)

    servers = cfg.get("servers", [])

    for s in servers:
        logging.info("[%s] Checking health...", s["name"])
        healthy, detail = health_check(s)

        if healthy:
            logging.info("[%s] Healthy: %s", s["name"], detail)
            continue

        logging.warning("[%s] Unhealthy: %s", s["name"], detail)
        code, out, err = restart_service(s)
        ok = (code == 0)

        msg = f"Restart {'OK' if ok else 'FAILED'}: exit={code}, out={out}, err={err}"
        logging.info("[%s] %s", s["name"], msg)

        notify_discord(f"⚠️ {s['name']} unhealthy\n{detail}\n{msg}")


# ---------------------- CLI Entry ----------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Python Remote Infra Orchestrator")
    parser.add_argument("--config", "-c", required=True, help="Path to config file (yaml/json)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_orchestrator(args.config)   