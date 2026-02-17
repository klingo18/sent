// MVP Resource 3: get_limit_order_quote
// Pre-trade: price, fee estimate, routing info.
// Section 32.6 — Params: token_address, amount_usdc, side

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const BASE_RPC = "https://mainnet.base.org";
const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const VIRTUAL = "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b";
const V2_ROUTER = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24";
const V3_POOL = "0x529d2863a1521d0b57db028168fdE2E97120017C";

// getAmountsOut(uint256,address[]) selector
const GET_AMOUNTS_OUT_SEL = "0xd06ca61f";
const SLOT0_SEL = "0x3850c7bd";
const TOKEN0_SEL = "0x0dfe1681";
const TOKEN1_SEL = "0xd21220a7";
const DECIMALS_SEL = "0x313ce567";

function encodeGetAmountsOut(amountIn: bigint, path: string[]): string {
  // ABI encode: uint256 amountIn, address[] path
  const amountHex = amountIn.toString(16).padStart(64, "0");
  const offsetHex = (64).toString(16).padStart(64, "0"); // offset to dynamic array
  const lenHex = path.length.toString(16).padStart(64, "0");
  const addrs = path.map((a) => a.replace("0x", "").toLowerCase().padStart(64, "0")).join("");
  return GET_AMOUNTS_OUT_SEL + amountHex + offsetHex + lenHex + addrs;
}

function decodeWord(hex: string, index: number): string {
  const clean = hex.startsWith("0x") ? hex.slice(2) : hex;
  return clean.slice(index * 64, index * 64 + 64);
}

function decodeUint256(hex: string, index: number): bigint {
  const word = decodeWord(hex, index);
  return BigInt("0x" + word);
}

function decodeAddress(hex: string, index: number): string {
  const word = decodeWord(hex, index);
  return "0x" + word.slice(24);
}

function decodeAmountsOut(hex: string): bigint[] {
  if (!hex || hex === "0x") return [];
  const length = Number(decodeUint256(hex, 1));
  const amounts: bigint[] = [];
  for (let i = 0; i < length; i += 1) {
    amounts.push(decodeUint256(hex, 2 + i));
  }
  return amounts;
}

async function ethCall(to: string, data: string): Promise<string> {
  const res = await fetch(BASE_RPC, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0", id: 1, method: "eth_call",
      params: [{ to, data }, "latest"],
    }),
  });
  const json = await res.json();
  if (json.error) throw new Error(json.error.message);
  return json.result ?? "0x";
}

async function getAmountsOut(amountIn: bigint, path: string[]): Promise<bigint[]> {
  const callData = encodeGetAmountsOut(amountIn, path);
  const result = await ethCall(V2_ROUTER, callData);
  return decodeAmountsOut(result);
}

async function getDecimals(address: string): Promise<number> {
  const result = await ethCall(address, DECIMALS_SEL);
  if (!result || result === "0x") return 18;
  return Number(BigInt(result));
}

async function getVirtualPriceUsd(): Promise<{ virtualPerUsdc: number; usdcPerVirtual: number }> {
  const [token0Hex, token1Hex, slot0Hex] = await Promise.all([
    ethCall(V3_POOL, TOKEN0_SEL),
    ethCall(V3_POOL, TOKEN1_SEL),
    ethCall(V3_POOL, SLOT0_SEL),
  ]);

  const token0 = decodeAddress(token0Hex, 0).toLowerCase();
  const token1 = decodeAddress(token1Hex, 0).toLowerCase();
  const sqrtPriceX96 = decodeUint256(slot0Hex, 0);

  const decimals0 = token0 === USDC.toLowerCase() ? 6 : 18;
  const decimals1 = token1 === VIRTUAL.toLowerCase() ? 18 : 6;

  const sqrtPrice = Number(sqrtPriceX96) / 2 ** 96;
  const priceRaw = sqrtPrice * sqrtPrice;
  const priceAdjusted = priceRaw * 10 ** (decimals0 - decimals1);

  if (priceAdjusted <= 0) {
    throw new Error("Invalid V3 price");
  }

  if (token0 === USDC.toLowerCase()) {
    const virtualPerUsdc = priceAdjusted;
    return { virtualPerUsdc, usdcPerVirtual: 1 / virtualPerUsdc };
  }

  const usdcPerVirtual = priceAdjusted;
  return { virtualPerUsdc: 1 / usdcPerVirtual, usdcPerVirtual };
}

serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  try {
    const url = new URL(req.url);
    const tokenAddress = url.searchParams.get("token_address") ?? "";
    const amountUsdc = parseFloat(url.searchParams.get("amount_usdc") ?? "10");
    const side = url.searchParams.get("side") ?? "buy";

    if (!tokenAddress || !tokenAddress.startsWith("0x") || tokenAddress.length !== 42) {
      return new Response(
        JSON.stringify({ error: "Invalid token_address" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 400 },
      );
    }

    if (amountUsdc < 1 || amountUsdc > 10000) {
      return new Response(
        JSON.stringify({ error: "amount_usdc must be 1-10000" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 400 },
      );
    }

    const tokenDecimals = await getDecimals(tokenAddress);
    const { virtualPerUsdc, usdcPerVirtual } = await getVirtualPriceUsd();

    // Spot price: tokens per 1 VIRTUAL (V2 quote)
    const oneVirtual = BigInt("1000000000000000000"); // 1e18
    const spotAmounts = await getAmountsOut(oneVirtual, [VIRTUAL, tokenAddress]);
    const tokensPerVirtualRaw = spotAmounts[spotAmounts.length - 1] ?? 0n;
    const tokensPerVirtual = Number(tokensPerVirtualRaw) / 10 ** tokenDecimals;

    // Estimate output for amount_usdc
    const estimatedVirtual = amountUsdc * virtualPerUsdc;
    const virtualAmountRaw = BigInt(Math.max(0, Math.floor(estimatedVirtual * 1e18)));
    const estAmounts = virtualAmountRaw > 0n
      ? await getAmountsOut(virtualAmountRaw, [VIRTUAL, tokenAddress])
      : [];
    const tokensOutRaw = estAmounts[estAmounts.length - 1] ?? 0n;
    const estimatedTokensOut = Number(tokensOutRaw) / 10 ** tokenDecimals;

    const effectivePerVirtual = estimatedVirtual > 0 ? estimatedTokensOut / estimatedVirtual : 0;
    const priceImpactPct = tokensPerVirtual > 0
      ? ((tokensPerVirtual - effectivePerVirtual) / tokensPerVirtual) * 100
      : 0;

    const serviceFee = Math.max(amountUsdc * 0.005, 0.50); // 0.5% or $0.50 min

    return new Response(
      JSON.stringify({
        tokenAddress,
        side,
        amountUsdc,
        estimatedTokensPerVirtual: tokensPerVirtual,
        estimatedTokensOut,
        estimatedVirtual,
        tokenDecimals,
        virtualPriceUsd: usdcPerVirtual,
        priceImpactPct: Number(priceImpactPct.toFixed(4)),
        routing: "USDC → VIRTUAL (V3 0.3%) → TOKEN (V2 0.3% + 1% tax)",
        fees: {
          serviceFee: serviceFee.toFixed(2),
          v3SwapFee: "0.3%",
          v2SwapFee: "0.3%",
          virtualsTax: "1.0%",
          totalEstimatedCost: "~1.6% of order value",
        },
        warnings: [
          ...(amountUsdc > 1000 ? ["Large order — price impact may be significant"] : []),
          ...(priceImpactPct > 5 ? ["High estimated price impact on leg 2"] : []),
        ],
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 200 },
    );
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    return new Response(
      JSON.stringify({ error: message }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 500 },
    );
  }
});
