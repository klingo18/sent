import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL") ?? "",
      Deno.env.get("SUPABASE_ANON_KEY") ?? "",
    );

    // Parse optional query params for filtering
    const url = new URL(req.url);
    const riskTier = url.searchParams.get("risk_tier");   // "low", "medium", "higher"
    const minTvl = parseFloat(url.searchParams.get("min_tvl") ?? "0");
    const minApy = parseFloat(url.searchParams.get("min_apy") ?? "0");
    const stablecoin = url.searchParams.get("stablecoin"); // e.g. "USDC"

    // Fetch recommendations (pre-ranked by scanner)
    let recsQuery = supabase
      .from("yield_recommendations")
      .select("*")
      .order("risk_tier")
      .order("rank_in_tier", { ascending: true });

    if (riskTier) {
      recsQuery = recsQuery.eq("risk_tier", riskTier);
    }
    if (minTvl > 0) {
      recsQuery = recsQuery.gte("tvl_usd", minTvl);
    }
    if (minApy > 0) {
      recsQuery = recsQuery.gte("apy", minApy);
    }

    const { data: recs, error: recsError } = await recsQuery;

    if (recsError) {
      return new Response(
        JSON.stringify({ error: recsError.message }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    // Optionally filter for specific stablecoin in symbol
    let filtered = recs ?? [];
    if (stablecoin) {
      const upper = stablecoin.toUpperCase();
      filtered = filtered.filter((r: Record<string, unknown>) =>
        ((r.symbol as string) ?? "").toUpperCase().includes(upper)
      );
    }

    // Group by tier for structured response
    const tiers: Record<string, unknown[]> = { low: [], medium: [], higher: [] };
    for (const rec of filtered) {
      const tier = (rec.risk_tier as string) ?? "higher";
      if (tiers[tier]) {
        tiers[tier].push({
          poolId: rec.pool_id,
          project: rec.project,
          symbol: rec.symbol,
          apyTotal: rec.apy,
          apyBase: rec.apy_base,
          apyReward: rec.apy_reward,
          tvlUsd: rec.tvl_usd,
          apyMean30d: rec.apy_mean_30d,
          ilRisk: rec.il_risk,
          exposure: rec.exposure,
          rankInTier: rec.rank_in_tier,
          url: rec.url,
        });
      }
    }

    // Also fetch pool-level metadata for the last update timestamp
    const { data: meta } = await supabase
      .from("yield_pools")
      .select("updated_at")
      .order("updated_at", { ascending: false })
      .limit(1);

    const lastUpdated = meta?.[0]?.updated_at ?? null;

    return new Response(
      JSON.stringify({
        chain: "Base",
        source: "DeFiLlama yields API",
        lastUpdated,
        tiers,
        strategies: [
          {
            key: "none",
            name: "No Yield (Hold USDC)",
            description: "USDC held in hot wallet. Zero risk, zero yield.",
            riskTier: "none",
          },
          {
            key: "auto_safe",
            name: "Auto-Route (Safe Lending)",
            description: "Automatically parks in highest-APY lending protocol (Aave, Moonwell, Morpho).",
            riskTier: "low",
          },
          {
            key: "auto_max",
            name: "Auto-Route (Max Yield)",
            description: "Highest available yield across all tiers including stable LPs.",
            riskTier: "medium",
          },
        ],
      }),
      {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      },
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ error: (err as Error).message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }
});
