import { AuthForm } from "@/components/auth-form";

import { getSafeAuthReturnHref } from "@/lib/auth-routing";

export default async function LoginPage({ searchParams }: Readonly<{
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}>) {
  const { next } = await searchParams;
  return <AuthForm mode="login" returnHref={getSafeAuthReturnHref(typeof next === "string" ? next : null)} />;
}
