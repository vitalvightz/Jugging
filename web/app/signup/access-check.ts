"use server";

import { cookies } from "next/headers";
import { hasSignupAccess, SIGNUP_ACCESS_COOKIE } from "@/lib/signup-access";

export async function checkSignupAccess(): Promise<boolean> {
  return hasSignupAccess((await cookies()).get(SIGNUP_ACCESS_COOKIE)?.value);
}
