// MVP Resource 2: is_token_supported
// Can we trade this token? Checks if a V2 pair exists for VIRTUAL/TOKEN.
// Section 32.6 — Params: token_address

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const BASE_RPC = "https://mainnet.base.org";
const VIRTUAL = "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b";
const V2_FACTORY = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6";
const ZERO_ADDR = "0x0000000000000000000000000000000000000000";

// getPair(address,address) selector
const GET_PAIR_SEL = "0xe6a43905";

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
  return json.result ?? "0x";
}

function padAddress(addr: string): string {
  return addr.replace("0x", "").toLowerCase().padStart(64, "0");
}

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  try {
    const url = new URL(req.url);
    const tokenAddress = url.searchParams.get("token_address") ?? "";

    if (!tokenAddress || !tokenAddress.startsWith("0x") || tokenAddress.length !== 42) {
      return new Response(
        JSON.stringify({ supported: false, error: "Invalid token_address parameter" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 400 },
      );
    }

    // Check V2 Factory for VIRTUAL/TOKEN pair
    const callData = GET_PAIR_SEL + padAddress(VIRTUAL) + padAddress(tokenAddress);
    const result = await ethCall(V2_FACTORY, callData);

    // Decode address from result (last 40 hex chars of 32-byte word)
    const pairAddress = "0x" + result.slice(-40);
    const hasPair = pairAddress.toLowerCase() !== ZERO_ADDR.toLowerCase() && result.length >= 66;

    return new Response(
      JSON.stringify({
        supported: hasPair,
        tokenAddress,
        pairAddress: hasPair ? pairAddress : null,
        dex: "uniswap_v2",
        chain: "base",
        routing: hasPair ? "USDC → VIRTUAL (V3 0.3%) → TOKEN (V2)" : null,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 200 },
    );
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    return new Response(
      JSON.stringify({ supported: false, error: message }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 500 },
    );
  }
});
