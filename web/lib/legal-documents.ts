// The Terms of Use and Privacy Notice, rendered in-app.
//
// Canonical source: docs/terms-of-use.md and docs/privacy-notice.md on the
// `docs/regulatory-intended-purpose` branch. The app ships its own rendering so
// the documents are readable *inside* the product — consent is only informed if
// the athlete can read what they are agreeing to at the moment they agree, and a
// repository markdown file is not reachable from a phone at signup.
//
// The canonical markdown now lives in this repo, so legal-documents.test.ts
// compares the two copies directly rather than pinning facts about a file it
// could not see. Any edit here needs the same edit in docs/privacy-notice.md or
// docs/terms-of-use.md, and the Service providers and International transfers
// paragraphs are compared verbatim.
//
// When either document changes, update the text here and bump the matching
// version in api/compliance.py and web/lib/compliance.ts. Note which version:
// TERMS_VERSION and HEALTH_CONSENT_VERSION gate acceptance, so bumping either
// re-collects it from every athlete; PRIVACY_NOTICE_VERSION gates nothing and
// is the one to bump when the notice is corrected.
//
// The remaining bracketed placeholder is real and intentional: no trading
// address has been decided, and inventing one would be worse than showing it is
// outstanding.

import { PRIVACY_NOTICE_VERSION, TERMS_VERSION } from "@/lib/compliance";

export type LegalSection = {
  heading: string;
  paragraphs?: string[];
  bullets?: string[];
  /** External documents the section refers to, rendered as links under it. */
  links?: LegalLink[];
};

export type LegalLink = { label: string; href: string };

export const YOUTUBE_TERMS_LINK: LegalLink = {
  label: "YouTube Terms of Service",
  href: "https://www.youtube.com/t/terms",
};
export const GOOGLE_PRIVACY_POLICY_LINK: LegalLink = {
  label: "Google Privacy Policy",
  href: "https://policies.google.com/privacy",
};

export type LegalDocument = {
  slug: string;
  title: string;
  version: string;
  /** Terms only: the date this version takes effect. */
  effectiveDate?: string;
  /** Privacy Notice only: when the wording was last revised. */
  lastUpdated?: string;
  status: string;
  intro: string;
  sections: LegalSection[];
};

export const TERMS_OF_USE: LegalDocument = {
  slug: "terms-of-use",
  title: "Terms of Use",
  version: TERMS_VERSION,
  effectiveDate: "19 August 2026",
  status: "Not ready for publication: the trading address is still to be inserted.",
  intro:
    "These Terms govern use of UNLXCK, operated by Michael Okafor, a sole trader trading as Unlxck (“UNLXCK”, “we”, “us”), of [TRADING ADDRESS]. By creating an account or using UNLXCK, you agree to these Terms. Our Privacy Notice explains how we use personal data. Health-data consent is requested separately and is not part of accepting these Terms.",
  sections: [
    {
      heading: "Eligibility",
      paragraphs: [
        "UNLXCK is intended for users aged 13 or over. Users must provide accurate age information. Additional privacy and safety protections apply to users under 18. UNLXCK does not currently support accounts for children under 13.",
      ],
    },
    {
      heading: "The service",
      paragraphs: [
        "UNLXCK provides personalised combat-sport training and performance support. Features may include training plans, readiness check-ins, training adaptation, injury-aware exercise restrictions, nutrition and weight-management guidance, progress features and notifications.",
        "UNLXCK may modify, restrict or withhold app-prescribed training when information supplied by the athlete triggers its safety rules.",
      ],
    },
    {
      heading: "Not medical care",
      paragraphs: [
        "UNLXCK supports training, performance, recovery awareness and risk-aware exercise selection. It does not diagnose, treat, cure or prevent disease or injury; provide medical diagnosis, prognosis or medical clearance; or replace a doctor, physiotherapist or other appropriately qualified healthcare professional.",
        "A warning, restriction or recommendation is precautionary. The absence of a warning does not mean that training, sparring or competing is medically safe. Seek appropriate professional or emergency help where circumstances require it.",
      ],
    },
    {
      heading: "Your responsibilities",
      bullets: [
        "Provide information that is reasonably accurate and current.",
        "Use the service and safety guidance responsibly.",
        "Stop training and obtain appropriate help where symptoms or circumstances require it.",
        "Keep account credentials secure.",
        "Do not misuse, interfere with, reverse engineer, circumvent security controls or unlawfully access the service or another user’s data.",
      ],
      paragraphs: [
        "Combat training and competition involve inherent risks. UNLXCK does not control sparring, coaching, competition or training performed outside the service.",
      ],
    },
    {
      heading: "AI and automated adaptation",
      paragraphs: [
        "UNLXCK uses software rules and may use AI-assisted systems to generate or adapt parts of training guidance. Outputs may be incomplete or incorrect. AI cannot override UNLXCK’s server-owned safety restrictions and must not be treated as a clinical assessment.",
        "Recommendations may change when you provide new information or when the service’s rules are updated.",
      ],
    },
    {
      heading: "Users under 18",
      paragraphs: [
        "Under-18 use is subject to UNLXCK’s Children & Age-Appropriate Use Policy. UNLXCK applies additional privacy and safety safeguards to child users. High-risk dehydration, water-cut or aggressive weight-cut protocols are not provided to under-18 users.",
      ],
    },
    {
      heading: "Accounts and suspension",
      paragraphs: [
        "You are responsible for activity on your account. We may restrict or suspend access where reasonably necessary for security, abuse prevention, serious breach of these Terms or protection of users or the service. Where appropriate, we will provide notice and a reasonable explanation.",
      ],
    },
    {
      heading: "Privacy and health data",
      paragraphs: [
        "Our Privacy Notice explains the personal data we collect, why we use it, recipients, retention and your rights. Where UNLXCK relies on explicit consent to process health information, that consent is obtained separately and may be withdrawn as explained in the Privacy Notice. Withdrawal may prevent health-dependent personalisation from continuing.",
      ],
    },
    {
      heading: "Intellectual property",
      paragraphs: [
        "UNLXCK and its software, branding, design and proprietary service content are protected by applicable intellectual-property rights. You retain rights in content you submit. You grant UNLXCK the limited rights necessary to host, process and use that content to operate the service in accordance with these Terms and the Privacy Notice.",
      ],
    },
    {
      heading: "Exercise demonstration videos",
      paragraphs: [
        "Some exercises include a demonstration video played from YouTube through the YouTube API Services. By using UNLXCK, you agree to be bound by the YouTube Terms of Service.",
        "Demonstration videos are provided by YouTube and the creators who publish them, not by UNLXCK, and a video may become unavailable at any time. A video shows how a movement looks; the prescription, cues and stop rules in your plan still apply.",
      ],
      links: [YOUTUBE_TERMS_LINK],
    },
    {
      heading: "Availability and changes",
      paragraphs: [
        "We may update, improve, remove or replace service features. We do not promise uninterrupted or error-free availability. Material changes affecting users will be communicated where required by law.",
      ],
    },
    {
      heading: "Liability",
      paragraphs: [
        "Nothing in these Terms excludes or limits liability where doing so would be unlawful, including liability that cannot lawfully be excluded under UK consumer law.",
        "To the extent permitted by law, UNLXCK is not responsible for losses that were not reasonably foreseeable when these Terms were agreed or that result from misuse of the service, inaccurate information supplied by a user, or activities outside UNLXCK’s reasonable control.",
        "Nothing in these Terms removes your statutory consumer rights.",
      ],
    },
    {
      heading: "Paid services",
      paragraphs: [
        "If UNLXCK introduces paid subscriptions or purchases, applicable price, payment, renewal, cancellation and refund information will be provided before purchase. Any additional paid-service terms will form part of the agreement where accepted.",
      ],
    },
    {
      heading: "Ending your account",
      paragraphs: [
        "You may stop using UNLXCK and request account deletion through the route identified in the Privacy Notice. We may terminate an account for serious or repeated breach, unlawful use or where reasonably necessary to protect the service or its users. Personal data following closure is handled under the Privacy Notice and retention policy.",
      ],
    },
    {
      heading: "Changes to these Terms",
      paragraphs: [
        "We may update these Terms as the service or law changes. Where a change materially affects your rights or obligations, we will provide appropriate notice and, where required, obtain fresh agreement.",
      ],
    },
    {
      heading: "Law and disputes",
      paragraphs: [
        "These Terms are governed by the laws of England and Wales, subject to any mandatory consumer protections that apply where you live. Nothing in these Terms removes rights you have under applicable consumer law.",
      ],
    },
    {
      heading: "Contact",
      paragraphs: ["Questions or complaints about these Terms can be sent to support@unlxck.com."],
    },
  ],
};

export const PRIVACY_NOTICE: LegalDocument = {
  slug: "privacy-notice",
  title: "Privacy Notice",
  version: PRIVACY_NOTICE_VERSION,
  lastUpdated: "2 November 2026",
  status: "Not ready for publication: the trading address is still to be inserted.",
  intro:
    "UNLXCK uses personal data to create, adapt and deliver personalised training guidance. This notice explains what we use, why, who receives it and your rights.",
  sections: [
    {
      heading: "Who we are",
      paragraphs: [
        "UNLXCK is the controller of personal data used to provide the UNLXCK service. UNLXCK is operated by Michael Okafor, a sole trader trading as Unlxck, of [TRADING ADDRESS].",
        "Privacy contact: support@unlxck.com",
      ],
    },
    {
      heading: "What we collect",
      bullets: [
        "Account and profile data such as name, email, age, sex, height, weight, sport, goals and schedule.",
        "Training data such as sessions, completion, RPE, notes and plans.",
        "Health-related data such as injuries, pain, soreness, fatigue, sleep, readiness, symptoms and recovery information.",
        "Nutrition and weight-management data, including bodyweight, target weight, appetite, supplements and caffeine.",
        "Feedback, screenshots and support information.",
        "Device, notification, security and diagnostic data.",
      ],
      paragraphs: ["Health information is special-category personal data under UK data-protection law."],
    },
    {
      heading: "Why we use it",
      bullets: [
        "Create and adapt training plans.",
        "Provide readiness, injury, recovery, nutrition and weight-management features.",
        "Restrict or change training where safety rules indicate this is appropriate.",
        "Explain training decisions and maintain training history.",
        "Send requested service and safety notifications.",
        "Operate accounts, prevent abuse, troubleshoot problems and improve UNLXCK.",
      ],
      paragraphs: [
        "UNLXCK is a performance and wellbeing service. It is not intended to diagnose, treat or replace professional medical care.",
      ],
    },
    {
      heading: "Lawful bases",
      paragraphs: [
        "We rely on four lawful bases, depending on what the processing is for.",
        "Article 6(1)(b) UK GDPR (contract): for the personal data we need in order to provide the service you have asked for: your account, profile, intake, plans, sessions and training history.",
        "Article 6(1)(f) UK GDPR (legitimate interests): for keeping UNLXCK secure and available, preventing abuse of signup and login, investigating faults, and improving the service. Our interests are running a secure, reliable product and making it work better for athletes. We have weighed those interests against your rights, and you can object to this processing at any time.",
        "Private beta usage analytics is included as agreed when joining the beta. We record app visits, feature opens, timer actions and successful saves to understand usage and fix problems. There is no separate optional analytics switch in this beta. Events contain only the feature, action, account identifier, server time and retry/run identifier, not health details, notes or nutrition contents. We do not add analytics cookies, device identifiers or session replay, or send events to an external analytics provider. Events are deleted after 180 days; account deletion also removes them. Contact us about your data rights using the details in this notice.",
        "Article 9(2)(a) UK GDPR (explicit consent): for health information: injuries, pain, soreness, fatigue, sleep, readiness and bodyweight, used for personalised training and safety features. This consent is asked for separately, is optional, and can be withdrawn at any time.",
        "We do not use your health information to improve UNLXCK, and we do not use it to train AI models. It is used only to build and adapt your own training and to apply safety rules to it. Where we look at how the product is performing, or decide what to build next, we do that without using health information at all. This applies to aggregated and anonymised forms of it too.",
        "Withdrawing consent does not affect processing already carried out lawfully, but UNLXCK may be unable to provide features that require health data afterwards.",
      ],
    },
    {
      heading: "AI and automated adaptation",
      paragraphs: [
        "UNLXCK uses software rules and AI-assisted processing to help generate and adapt training guidance from the information you provide. Relevant plan context may be sent to service providers supporting this processing.",
        "With health-data consent on, the notes you write after training (a sparring note, a manual session's takeaway or a completed session's notes) may be sent to OpenAI with that day's session types, exercise names and what you logged for them, to suggest one technique focus for your next matching session. Your name, email and profile details are not added, but the note itself is sent as you wrote it. We screen notes for pain, injury, head-impact and other health language and do not send the ones we recognise, but no screen catches every wording, so please keep health details and other people's names out of these notes. A suggestion never changes your training load. Suggestions are stored separately from your notes, can be dismissed, and stop when you withdraw consent.",
        "These systems may change, restrict or withhold training guidance. We have assessed whether that is a decision based solely on automated processing with legal or similarly significant effects under Article 22 UK GDPR, and concluded it is not: it changes what training UNLXCK suggests to you, not your legal position or your access to anything comparable, and you remain free to train differently. We keep that assessment under review, and apply it more cautiously to athletes under 18.",
        "If you think an automated adaptation has got something wrong, contact us using the privacy contact above and a person will look at it.",
      ],
    },
    {
      heading: "Service providers",
      paragraphs: [
        "We use trusted service providers to run UNLXCK. We share only the information each one needs to provide its part of the service, and require appropriate data-protection safeguards from all of them.",
      ],
      bullets: [
        "Supabase: sign-in, database and file storage. Holds your account, profile, training, health and feedback data, on servers in the EU.",
        "OpenAI: AI-assisted plan generation and training-focus suggestions. Receives the plan context needed to build your training, which can include injury and restriction information, and, with health-data consent, your training notes with that day's session and exercise details. Your data is not used to train their models.",
        "Vercel: hosting for the UNLXCK website and app. Handles request and device information.",
        "Hetzner: hosting for our backend and plan-generation service, on servers in Germany.",
        "Resend: service and feedback email.",
        "Cloudflare Turnstile: blocks automated abuse of signup and login. Receives device and network signals, never health information.",
        "Sentry: error diagnostics, used to find and fix faults. Receives technical information about what went wrong. Injury, pain, readiness and similar information is filtered out before a report is sent, and UNLXCK does not record your screen or session.",
      ],
    },
    {
      heading: "International transfers",
      paragraphs: [
        "Some service providers may process limited data outside the UK. Where this creates a restricted transfer, we use approved safeguards, such as the UK Addendum to the Standard Contractual Clauses, and keep a record of the safeguard that applies. You can ask us for a copy of the safeguard we rely on using the privacy contact above.",
      ],
    },
    {
      heading: "YouTube demonstration videos",
      paragraphs: [
        "Some exercises include a demonstration video from YouTube, which UNLXCK shows using the YouTube API Services. An exercise with a video shows a preview image loaded from YouTube’s servers. The YouTube player itself loads only when you tap play, in YouTube’s privacy-enhanced mode.",
        "When a preview image or the player loads, Google, which operates YouTube, receives your IP address and technical information about your device and browser. Once you play a video, YouTube may store or read information on your device, such as cookies or local storage, and collect information about how you use the player. Google handles that information as a separate controller under its own policies, not as a service provider acting for UNLXCK. The Google Privacy Policy and the YouTube Terms of Service explain how.",
        "UNLXCK does not send Google your name, email, training, health or other account information, and does not receive personal information about you from YouTube. UNLXCK remembers on your device which videos you have already watched, so they can be shown smaller next time; that record stays on your device. If you do not want YouTube to receive information from the player, do not tap play. Every exercise, including its cues and stop rules, works without the video.",
      ],
      links: [GOOGLE_PRIVACY_POLICY_LINK, YOUTUBE_TERMS_LINK],
    },
    {
      heading: "How long we keep data",
      paragraphs: [
        "We keep identifiable data only for as long as we need it, then delete it or irreversibly anonymise it.",
        "While your account is open we keep your profile, plans, training history, injury, readiness and nutrition data, so the service works and your history is there when you come back to it.",
        "If you ask us to delete your data, we do it within one month. You can ask at any time, for any reason or none. See “Your rights”.",
        "Beta screenshots are kept for no more than 90 days. Feedback is kept while it is needed for support and product improvement, then anonymised or deleted.",
        "For closed and long-inactive accounts, we keep data only while it is still needed to provide the service or to meet a legal obligation. Security and audit logs are kept only for as long as their security purpose requires. Backups are removed on their normal expiry cycle, and data deleted from the live service is not restored back into ordinary use.",
      ],
    },
    {
      heading: "Your rights",
      bullets: [
        "Access your personal data.",
        "Correct inaccurate data.",
        "Request deletion.",
        "Restrict processing.",
        "Receive portable data.",
        "Object to processing we carry out under legitimate interests.",
        "Withdraw consent at any time where processing relies on consent.",
      ],
      paragraphs: [
        "The right to object applies to the processing we carry out under legitimate interests: security, abuse prevention, fault investigation and service improvement. Tell us and we will stop, unless we can show compelling grounds that override your interests. It does not cover what we have to process to provide the service under our agreement with you; for health information, withdraw your consent instead, which you can do at any time without giving a reason.",
        "Requests can be made using the privacy contact above, or from Settings → Privacy. We may verify identity where reasonably necessary.",
      ],
    },
    {
      heading: "Users under 18",
      paragraphs: [
        "Under-18 accounts get high-privacy defaults and additional safety rules. UNLXCK does not provide aggressive weight-cut, dehydration or water-cut guidance to athletes under 18.",
      ],
    },
    {
      heading: "How to reach us",
      paragraphs: [
        "You can exercise any of these rights, or ask a question about this notice, using the privacy contact at the top of this page. In the app you can also review and change your health-data consent, and request account deletion, from Settings → Privacy.",
      ],
    },
    {
      heading: "Complaints",
      paragraphs: [
        "Please contact UNLXCK first if you have a privacy concern. You also have the right to complain to the UK Information Commissioner’s Office (ICO).",
      ],
    },
    {
      heading: "Changes",
      paragraphs: [
        "We will review this notice when our data uses, processors or legal requirements materially change. Where a new use of your personal data requires notice, we will tell you before that processing begins.",
      ],
    },
  ],
};

export const LEGAL_DOCUMENTS: readonly LegalDocument[] = [TERMS_OF_USE, PRIVACY_NOTICE];

export const TERMS_HREF = `/legal/${TERMS_OF_USE.slug}`;
export const PRIVACY_HREF = `/legal/${PRIVACY_NOTICE.slug}`;
export const SUPPORT_EMAIL = "support@unlxck.com";

/**
 * Where a data-rights request goes.
 *
 * support@unlxck.com is the default public privacy/support route. Deployments
 * may override it through NEXT_PUBLIC_PRIVACY_CONTACT_EMAIL if the contact ever
 * changes without needing a code release.
 */
export function getPrivacyContactEmail(): string {
  return (process.env.NEXT_PUBLIC_PRIVACY_CONTACT_EMAIL ?? "").trim() || SUPPORT_EMAIL;
}

export function buildDataRequestMailto(subject: string, body: string): string | null {
  const address = getPrivacyContactEmail();
  if (!address) {
    return null;
  }
  return `mailto:${address}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
}