#!/usr/bin/env python3
"""
CranL CLI Management Tool for inventory-prototype.
Reads CRANL_API_TOKEN and CRANL_APP_ID from environment or .env file.
"""

import os
import sys
import json
import argparse
import urllib.request
import urllib.error
import ssl

def load_env():
    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    if os.path.exists(env_file):
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    if k.strip() not in os.environ:
                        os.environ[k.strip()] = v.strip()

load_env()

API_TOKEN = os.environ.get('CRANL_API_TOKEN', '')
APP_ID = os.environ.get('CRANL_APP_ID', '0a42b8bc-6068-46de-89fb-7918d71f78a9')
BASE_URL = 'https://app.cranl.com/api/v1'

def api_request(endpoint, method='GET', data=None):
    if not API_TOKEN:
        print("Error: CRANL_API_TOKEN not found in environment or .env file.", file=sys.stderr)
        sys.exit(1)
        
    url = f"{BASE_URL}{endpoint}"
    headers = {
        'Authorization': f'Bearer {API_TOKEN}',
        'Content-Type': 'application/json'
    }
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    payload = json.dumps(data).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(req, context=ctx) as response:
            res_data = response.read().decode('utf-8')
            return json.loads(res_data) if res_data else {}
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8', errors='replace')
        print(f"HTTP Error {e.code}: {err_msg}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Request failed: {e}", file=sys.stderr)
        sys.exit(1)

def cmd_status(args):
    data = api_request(f"/apps/{APP_ID}")
    print(f"App Name:    {data.get('name')}")
    print(f"App ID:      {data.get('id')}")
    print(f"Status:      {data.get('status')}")
    print(f"Region:      {data.get('region')}")
    print(f"Public URL:  {data.get('url')}")
    print(f"Git Repo:    {data.get('source', {}).get('repository')} ({data.get('source', {}).get('branch')})")
    print(f"Updated At:  {data.get('updated_at')}")

def cmd_deployments(args):
    data = api_request(f"/apps/{APP_ID}/deployments")
    deployments = data.get('deployments', [])
    print(f"{'Deployment ID':<24} | {'Status':<10} | {'Started At':<22} | {'Title'}")
    print("-" * 80)
    for d in deployments[:args.limit]:
        title = (d.get('title') or '')[:40]
        print(f"{d.get('id'):<24} | {d.get('status'):<10} | {d.get('started_at', '')[:19]:<22} | {title}")

def cmd_logs(args):
    data = api_request(f"/apps/{APP_ID}/logs")
    lines = data.get('lines', [])
    if not lines:
        print("No recent log lines found.")
        return
    for line in lines[-args.lines:]:
        if isinstance(line, dict):
            t = line.get('time', '')[11:19]
            lvl = line.get('level', 'info').upper()
            txt = line.get('text', '')
            print(f"[{t}] [{lvl}] {txt}")
        else:
            print(line)

def cmd_health(args):
    data = api_request(f"/apps/{APP_ID}/health-check", method='POST', data={'path': args.path})
    passed = data.get('passed', False)
    status = data.get('status')
    latency = data.get('latency_ms')
    url = data.get('url')
    print(f"Health Check: {'PASS' if passed else 'FAIL'} (HTTP {status})")
    print(f"URL:          {url}")
    print(f"Latency:      {latency} ms")

def cmd_deploy(args):
    print(f"Triggering deployment for app {APP_ID}...")
    data = api_request(f"/apps/{APP_ID}/deploy", method='POST')
    print("Deployment triggered successfully:")
    print(json.dumps(data, indent=2))

def main():
    parser = argparse.ArgumentParser(description="CranL CLI tool for inventory-prototype")
    subparsers = parser.add_subparsers(dest='command', help='Sub-commands')

    p_status = subparsers.add_parser('status', help='Get app status and details')
    p_status.set_defaults(func=cmd_status)

    p_dep = subparsers.add_parser('deployments', help='List recent deployments')
    p_dep.add_argument('--limit', type=int, default=5, help='Number of deployments to list')
    p_dep.set_defaults(func=cmd_deployments)

    p_logs = subparsers.add_parser('logs', help='Get runtime container logs')
    p_logs.add_argument('--lines', type=int, default=30, help='Number of log lines')
    p_logs.set_defaults(func=cmd_logs)

    p_health = subparsers.add_parser('health', help='Perform health check')
    p_health.add_argument('--path', type=str, default='/login', help='Path to check')
    p_health.set_defaults(func=cmd_health)

    p_deploy = subparsers.add_parser('deploy', help='Trigger new deployment on CranL')
    p_deploy.set_defaults(func=cmd_deploy)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)

if __name__ == '__main__':
    main()
