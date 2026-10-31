import { checkSignupAccess } from "@/app/signup/access-check";
import { getApprovedProfile } from "@/lib/api";

// Existing approved members never need an invite code again.
export async function hasSignupAdmission(token: string): Promise<boolean> {
  const { approved } = await getApprovedProfile(token);
  return approved || checkSignupAccess();
}
