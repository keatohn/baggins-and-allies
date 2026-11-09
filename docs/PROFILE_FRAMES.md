# Profile frames

Use a transparent PNG, ideally 512 × 512 pixels, with a circular opening in the center for the player's portrait. Keep the decorative frame inside the canvas. Slightly non-square images are supported and retain their aspect ratio.

For repo-hosted artwork, add the image to `frontend/public/frames/` and deploy the frontend. Its public path is `/frames/<filename>.png`. The supplied frames are `steel.png`, `gold.png`, `mithril.png`, `secret_fire.png`, and `silmaril.png`.

In **Admin → Users → Tier & XP → Frames**, add a frame, enter a label and its public path (for example `/frames/gold.png`), select it in a tier's **Frame** dropdown, and save. Saving configuration does not upload image files. You can also use an HTTPS image URL hosted elsewhere, or an existing `/avatar-frames/<filename>` path.

Assigned frames appear around portraits in the account menu, profile, friends list, leaderboard, player cards, and turn ticker. The tier's color/glow ring remains the fallback while artwork loads or if it fails to load. Removing a frame from the catalog clears its tier assignments.
