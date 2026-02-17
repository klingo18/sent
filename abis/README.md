# ABIs

## JSON ABI Files (canonical, from official sources)

| File | Contract | Address (Base) | Source |
|------|----------|---------------|--------|
| `erc20.json` | ERC-20 standard | any | OpenZeppelin |
| `uniswap_v2_router02.json` | Uniswap V2 Router02 | `0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24` | [unpkg @uniswap/v2-periphery](https://unpkg.com/@uniswap/v2-periphery@1.1.0-beta.0/build/IUniswapV2Router02.json) |
| `uniswap_v2_factory.json` | Uniswap V2 Factory | `0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6` | [Uniswap docs](https://docs.uniswap.org/contracts/v2/reference/smart-contracts/factory) |
| `uniswap_v2_pair.json` | Uniswap V2 Pair | per-token | [Uniswap docs](https://docs.uniswap.org/contracts/v2/reference/smart-contracts/pair) |
| `uniswap_v3_swaprouter02.json` | Uniswap V3 SwapRouter02 | `0x2626664c2603336E57B271c5C0b26F421741e481` | [BaseScan verified](https://basescan.org/address/0x2626664c2603336E57B271c5C0b26F421741e481) |
| `multicall3.json` | Multicall3 | `0xcA11bde05977b3631167028862bE2a173976CA11` | [GitHub mds1/multicall](https://github.com/mds1/multicall) |
| `aave_v3_pool.json` | Aave V3 Pool | `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5` | [Aave docs](https://aave.com/docs/aave-v3/smart-contracts/pool) |

## Still Inline (to be migrated)

| Contract | Location | Used For |
|----------|----------|----------|
| V3 Pool (slot0, liquidity) | `engine/pricing/poller.py`, `engine/pricing/keys.py` | Multicall price reads |

## Usage

Python modules currently use inline ABI snippets. Phase 2 will load from these JSON files:
```python
import json, pathlib
ABI_DIR = pathlib.Path(__file__).parent / "abis"
V2_ROUTER_ABI = json.loads((ABI_DIR / "uniswap_v2_router02.json").read_text())
```
