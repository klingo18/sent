"""
SENTINEL ACP Connection Test.

Verifies:
  1. SDK imports work
  2. VirtualsACP connects (Socket.IO)
  3. Can fetch agent info + offerings
  4. Can list active jobs

Usage:
    python3 scripts/test_acp_connection.py

Requires: .env with SENTINEL_WALLET, SENTINEL_PRIVATE_KEY, SENTINEL_ENTITY_ID
"""

import sys
import os
import time

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


def main():
    print("=" * 60)
    print("  SENTINEL — ACP Connection Test")
    print("=" * 60)
    print()

    # 1. Check env vars
    wallet = os.getenv("SENTINEL_WALLET", "")
    key = os.getenv("SENTINEL_PRIVATE_KEY", "")
    entity_id = int(os.getenv("SENTINEL_ENTITY_ID", "0"))

    if not wallet or wallet == "0x...":
        print("[FAIL] SENTINEL_WALLET not set in .env")
        sys.exit(1)
    if not key or key == "0x...":
        print("[FAIL] SENTINEL_PRIVATE_KEY not set in .env")
        sys.exit(1)
    if entity_id == 0:
        print("[FAIL] SENTINEL_ENTITY_ID not set in .env")
        sys.exit(1)

    print(f"[OK] Wallet:    {wallet}")
    print(f"[OK] Entity ID: {entity_id}")
    print()

    # 2. Import SDK
    try:
        from virtuals_acp.client import VirtualsACP
        from virtuals_acp.contract_clients.contract_client_v2 import ACPContractClientV2
        print("[OK] virtuals_acp SDK imported")
    except ImportError as e:
        print(f"[FAIL] Cannot import virtuals_acp: {e}")
        print("  Run: pip install virtuals-acp")
        sys.exit(1)

    # 3. Create contract client
    try:
        contract_client = ACPContractClientV2(
            wallet_private_key=key,
            agent_wallet_address=wallet,
            entity_id=entity_id,
        )
        print(f"[OK] ACPContractClientV2 created")
        print(f"     Agent address: {contract_client.agent_wallet_address}")
    except Exception as e:
        print(f"[FAIL] ACPContractClientV2 init failed: {e}")
        sys.exit(1)

    # 4. Create VirtualsACP (this connects Socket.IO)
    events_received = []

    def on_new_task(job, memo):
        events_received.append(("new_task", job.id))
        print(f"  [EVENT] onNewTask job={job.id}")

    def on_evaluate(job):
        events_received.append(("evaluate", job.id))
        print(f"  [EVENT] onEvaluate job={job.id}")

    print()
    print("[...] Creating VirtualsACP (connects Socket.IO)...")
    try:
        acp = VirtualsACP(
            acp_contract_clients=contract_client,
            on_new_task=on_new_task,
            on_evaluate=on_evaluate,
        )
        print(f"[OK] VirtualsACP created")
        print(f"     Agent address: {acp.agent_address}")
    except Exception as e:
        print(f"[FAIL] VirtualsACP init failed: {e}")
        sys.exit(1)

    # 5. Fetch agent info
    print()
    print("[...] Fetching agent info...")
    try:
        agent = acp.get_agent(wallet)
        if agent:
            print(f"[OK] Agent found:")
            print(f"     Name:       {getattr(agent, 'name', 'N/A')}")
            print(f"     Wallet:     {getattr(agent, 'wallet_address', wallet)}")
            offerings = getattr(agent, 'job_offerings', [])
            print(f"     Offerings:  {len(offerings)}")
            for off in offerings:
                print(f"       - {off.name} @ ${off.price} ({off.price_type})")
            resources = getattr(agent, 'resource_offerings', [])
            print(f"     Resources:  {len(resources)}")
            for res in resources:
                print(f"       - {res.name}: {res.url[:50]}...")
        else:
            print("[WARN] Agent not found on ACP platform")
            print("       → Register at https://app.virtuals.io → Agent → ACP")
    except Exception as e:
        print(f"[WARN] Could not fetch agent: {e}")

    # 6. Check active jobs
    print()
    print("[...] Checking active jobs...")
    try:
        jobs = acp.get_active_jobs()
        print(f"[OK] Active jobs: {len(jobs or [])}")
        for job in (jobs or [])[:5]:
            phase = job.phase.name if hasattr(job.phase, "name") else str(job.phase)
            print(f"     Job {job.id}: phase={phase} name={getattr(job, 'name', 'N/A')}")
    except Exception as e:
        print(f"[WARN] Could not fetch jobs: {e}")

    # 7. Wait briefly for any Socket.IO events
    print()
    print("[...] Listening for Socket.IO events (5s)...")
    time.sleep(5)
    if events_received:
        print(f"[OK] Received {len(events_received)} events")
    else:
        print("[OK] No events (normal if no pending jobs)")

    print()
    print("=" * 60)
    print("  ACP CONNECTION TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
