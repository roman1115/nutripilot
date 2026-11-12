# Frontend Refresh - 2026-10-23

## Scope

- Replace the dark developer workspace with a light personal meal workspace.
- Keep everyday navigation separate from trace and evaluation tools.
- Show conversation history by default at desktop widths of 1100px and above.
  Use an overlay with a dismiss action on smaller screens.
- Use local food photography for conversation starters. Photographs are
  illustrative and are not associated with recommended meal records.
- Keep execution details in a dismissible drawer. Open the drawer automatically
  for pending confirmation and close it after successful confirmation or refusal.
- Make citations expandable, while keeping safety warnings visible.
- Use consistent typography, spacing and unframed sections for preferences,
  saved plans, traces and evaluations.

## Validation

- TypeScript checking and Vite production build.
- Browser regression against the local Fake model service: recommendations,
  trace navigation, nutrition aliases, message restoration, history search,
  pending plan preview and approved plan persistence.
- Responsive checks at 1440, 1024, 768, 390 and 320px; starter images must load
  and the document must have no horizontal overflow.
- Desktop and mobile screenshots reviewed separately.

Screenshots and regression results are in the ignored directory
`.runtime/redesign-2026-10-23/`.

The visual refresh does not change backend recommendation logic, user profile
provenance, food data or authentication. It does not validate real model quality;
the browser regression uses the Fake model to avoid paid calls for layout work.
