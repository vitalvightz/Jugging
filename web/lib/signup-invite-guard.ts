/** Only accounts previously rejected for lacking an invite need rechecking. */
export const INVITE_PENDING_FLAG = "invite_pending";
export const INVITE_REQUIRED_HREF = "/signup?invite=required";

export type InviteGuardSession = { created_at?: string | null; invite_pending?: boolean };

export function needsInviteCheck(session: InviteGuardSession): boolean {
  return session.invite_pending === true;
}
