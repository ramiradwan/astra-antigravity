# Changelog

## Companion 0.4.2

- Remove the copied Windows maintenance helper after uninstall.
- Retry transient Windows status-file sharing without interrupting pairing.
- Update the version-pinned setup package to 0.4.2.

## HTTPS 0.17.0

- Offer a version-pinned GitHub bootstrap command when its public release and hash have been verified and configured.
- Preserve a saved task's request ID through computer setup and reject unsupported setup commands.
- Prepare headless companion 0.4.1 with portable maintenance isolation, and retain the previous ChatGPT UI for existing cards.

## HTTPS 0.16.2

- Route support and privacy contact links to the publisher's LinkedIn profile through `/support`.
- Keep the support redirect public, read-only and fixed to that profile, without forwarding query parameters or referrer data.
- Update support disclosures to match the LinkedIn contact channel.

## HTTPS 0.16.1

- Describe remote task actions as `delegate_task` and `continue_task`; retain callable legacy aliases for existing clients without duplicate launches.
- Keep installed companion maintenance controls unavailable to portable instances, enforced by the local controller and relay.
- Support versioned, hash-pinned review recordings on the existing Free static asset deployment.

## HTTPS 0.16.0

- Remove browser file-download fallbacks and executable-download buttons from the current interface.
- Transfer approved files through supported ChatGPT APIs; otherwise retain the saved file and explain that transfer is unavailable.
- Align hosted sign-in and setup pages with the headless companion and controls in ChatGPT.
- Make ChatGPT the front page's primary action and label account access as secondary; disclose preview setup availability.
- Retire old browser download pages without starting a transfer; keep private one-use file grants for host-mediated delivery.
- Encode file grants as URL-safe capabilities so the current ChatGPT card accepts genuine relay links; preserve outstanding grants and one-use checks.

## HTTPS 0.15.1

- Keep the computer panel usable when a saved setup task was cancelled before a computer was assigned.
- Open the Computers tab from the computer entry point and retain the previous cached UI resource.
- Preserve the computer-management and account-control permissions in the packaged OAuth defaults; existing connections keep their granted permissions.
- Mark task cancellation as a potentially destructive action in the tool catalog.

## HTTPS 0.15.0 / companion 0.4.0 (release candidates)

- Manage computers, provider setup, workspace permission, Pause/Resume, task cancellation and removal in ChatGPT.
- Run the companion headlessly with no GUI or local web listener; prepare a verified npx bootstrap.
- Separate until-revoked approval from rotating device credentials and preserve existing 30-day grants until explicit conversion.
- Request host-mediated downloads up to 20 MiB, with in-card preparation and a browser fallback for unsupported hosts.
- Verify idle companion updates with publisher signatures and hashes, retain rollback, and provide guarded Windows/ChatGPT uninstall.
- Keep machine-operation receipts and UI refreshes outside the conversation model's context.

## 0.9.2 / HTTPS 0.14.1 / companion 0.3.1 (earlier candidates)

- Use Delegate to Antigravity with plain task and connection labels.
- Add optional Entra authentication, custom authority validation and directory enrollment hooks.
- Add responsive light/dark UI source and a synthetic accessibility gallery.

## 0.9.1

- Prepare MIT-licensed desktop distribution with icons, listing metadata, dependency locking and release validation.
- Adopt portable Agent Plugins 1.0.0 with root manifests and OpenAI metadata in its extension namespace.
- Add reproducible plugin ZIPs and checksums and installation/support guidance.
- Document data handling, local execution, third-party authentication and the supported background-delivery boundary.
- Bundle native hashed-wheel dependencies and original licenses for Windows/Linux CPython 3.10 and 3.13; verify extracted gateway startup without uv.
- Declare each public tool's read/write, deletion and external-service behavior explicitly for marketplace review.
- Add an opt-in bundled-release reviewer recorder with independent output checks, saved-result recovery, and a labeled CLI playback video with captions.

## 0.9.0

- Add implementation, analysis, supplied-source research and document task types.
- Preserve task contracts through corrections and recovery; check explicit capabilities, source references and document artifacts.

## 0.8.0

- Run delegated tasks in owned background jobs and push compact durable decisions/results.
- Add acknowledgement, replay, deduplication and cancellation by delegation receipt.

## 0.7.0

- Route light/heavy tasks by advertised Antigravity model profiles.
- Offer Opus 5.5 explicitly when Gemini 4 Argon High is unavailable; preserve model pins.
