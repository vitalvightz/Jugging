import { checkSignupAccess } from "@/app/signup/access-check";
import { getSupabaseBrowserClient } from "@/lib/supabase";

const ADMITTED_KEY_PREFIX = "unlxck-admitted:";

// An approved profile survives browser changes; new members still need the code.
export async function hasSignupAdmission(userId: string | null | undefined): Promise<boolean> {
  if (!userId) throw new Error("Missing authenticated user ID");
  const { data, error } = await getSupabaseBrowserClient()
    .from("profiles").select("access_status").eq("id", userId).maybeSingle().overrideTypes<{ access_status: string | null }, { merge: false }>();
  if (error) throw error;
  return data?.access_status === "approved" || checkSignupAccess();
}

/**
 * Whether this browser last saw `userId` admitted. A hint only: it lets the
 * app start the athlete's own reads while admission is re-checked, never
 * replaces that check, and nothing is shown until it passes.
 */
export function wasAdmittedHere(userId: string | null | undefined): boolean {
  if (!userId) return false;
  try {
    return window.localStorage.getItem(`${ADMITTED_KEY_PREFIX}${userId}`) === "1";
  } catch {
    return false;
  }
}

export function rememberAdmission(userId: string | null | undefined, admitted: boolean): void {
  if (!userId) return;
  try {
    if (admitted) window.localStorage.setItem(`${ADMITTED_KEY_PREFIX}${userId}`, "1");
    else window.localStorage.removeItem(`${ADMITTED_KEY_PREFIX}${userId}`);
  } catch {
    // Storage unavailable: admission simply runs before the reads, as before.
  }
}
