import { checkSignupAccess } from "@/app/signup/access-check";
import { getSupabaseBrowserClient } from "@/lib/supabase";

// An approved profile survives browser changes; new members still need the code.
export async function hasSignupAdmission(userId: string | null | undefined): Promise<boolean> {
  if (!userId) throw new Error("Missing authenticated user ID");
  const { data, error } = await getSupabaseBrowserClient()
    .from("profiles").select("access_status").eq("id", userId).maybeSingle().overrideTypes<{ access_status: string | null }, { merge: false }>();
  if (error) throw error;
  return data?.access_status === "approved" || checkSignupAccess();
}
