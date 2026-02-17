// MVP Resource 1: get_service_status
// Is SENTINEL online + healthy?
// Section 32.6 — No params required.

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

    // Count active orders
    const { count: activeOrders } = await supabase
      .from("orders")
      .select("*", { count: "exact", head: true })
      .not("status", "in", '("completed","cancelled","expired","failed")');

    // Count total completed
    const { count: totalCompleted } = await supabase
      .from("orders")
      .select("*", { count: "exact", head: true })
      .eq("status", "completed");

    return new Response(
      JSON.stringify({
        status: "online",
        version: "0.1.0-mvp",
        chain: "base",
        chainId: 8453,
        activeOrders: activeOrders ?? 0,
        totalCompleted: totalCompleted ?? 0,
        maxConcurrentOrders: 50,
        supportedOrderTypes: ["limit_order", "cancel_order"],
        priceCheckIntervalSeconds: 3,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 200 },
    );
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    return new Response(
      JSON.stringify({ status: "error", message }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 500 },
    );
  }
});
