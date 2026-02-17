// MVP Resource 5: get_active_orders
// All open orders for a user by clientAddress (buyer wallet).
// Section 32.6 — Params: clientAddress

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL") ?? "",
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "",
    );

    const url = new URL(req.url);
    const clientAddress = url.searchParams.get("clientAddress") ?? "";

    if (!clientAddress || !clientAddress.startsWith("0x")) {
      return new Response(
        JSON.stringify({ error: "Missing or invalid clientAddress parameter" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 400 },
      );
    }

    const { data: orders, error } = await supabase
      .from("orders")
      .select("*")
      .eq("buyer_address", clientAddress.toLowerCase())
      .not("status", "in", '("completed","cancelled","expired","failed")')
      .order("created_at", { ascending: false });

    if (error) throw error;

    return new Response(
      JSON.stringify({
        clientAddress,
        activeOrders: (orders ?? []).map((o: any) => ({
          id: o.id,
          jobId: o.job_id,
          tokenAddress: o.token_address,
          tokenSymbol: o.token_symbol,
          side: o.side,
          status: o.status,
          usdcAmount: o.usdc_amount,
          limitPriceUsd: o.limit_price_usd,
          createdAt: o.created_at,
        })),
        count: orders?.length ?? 0,
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
