import { CoachWorkspace } from "@/components/coach-workspace";
import { notFound } from "next/navigation";

export default async function FighterPage({
  params,
}: {
  params: Promise<{ athleteId: string }>;
}) {
  const { athleteId } = await params;
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      athleteId,
    )
  )
    notFound();
  return <CoachWorkspace fighterId={athleteId} />;
}
