.PHONY: bootstrap run lint test clean

bootstrap:
	python3 -m venv venv
	. venv/bin/activate && pip install -r requirements.txt
	@echo ""
	@echo "Done. Now: cp .env.example .env && edit .env"

run:
	python3 main.py

lint:
	python3 -m py_compile main.py
	python3 -m py_compile engine/config.py
	python3 -m py_compile engine/acp/handler.py
	python3 -m py_compile engine/acp/offerings.py
	python3 -m py_compile engine/acp/deliverable.py
	python3 -m py_compile engine/executor/swap_v2.py
	python3 -m py_compile engine/orders/state_machine.py
	python3 -m py_compile engine/wallets/hot_wallet.py
	python3 -m py_compile engine/db/client.py
	python3 -m py_compile engine/pricing/keys.py
	python3 -m py_compile engine/pricing/registry.py
	python3 -m py_compile engine/pricing/poller.py
	python3 -m py_compile engine/pricing/pricer.py
	python3 -m py_compile engine/pricing/triggers.py
	python3 -m py_compile engine/pricing/service.py
	python3 -m py_compile engine/tokens/resolver.py
	python3 -m py_compile engine/rpc/pool.py
	python3 -m py_compile scripts/register_offerings.py
	python3 -m py_compile scripts/test_acp_connection.py
	@echo "lint ok"

test:
	python3 main.py --smoke

clean:
	rm -rf venv __pycache__ engine/__pycache__ engine/**/__pycache__
