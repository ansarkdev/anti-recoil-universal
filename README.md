Gemini said
📜 README: Anti-Recoil Universal
<details>
<summary><b>⚠️ Anti-Cheat Warning</b></summary>

You will be banned on games with decent anti-cheats (Apex, Valorant, etc.). This script currently lacks any hardware spoofing or bypasses. Use this only for testing or on games with basic protection.

</details>

<details>
<summary><b>🚀 Installation</b></summary>

Requirements: Windows and Python 3.

Install Dependencies: ```bash
pip install pynput FreeSimpleGUI

Run: ```bash
python recoil.py

</details>

<details>
<summary><b>🎮 Setup & Usage</b></summary>

Profiles: Create gun profiles using the + button.

Speed: Adjust how fast the mouse pulls down.

Distance: Set how far the mouse travels; use 0 for an infinite pull.

Saving: Hit the SAVE button to lock in your profile settings.

Switching: Press F6 (default) to cycle through your guns while in-game.

OSD: The green overlay shows your current active weapon.

</details>

<details>
<summary><b>⚙️ Key Features</b></summary>

RMB Gate: If checked, the script only pulls when you are aiming down sights (holding Right Click).

Direct Input: Uses Win32 APIs for hardware-level mouse movement.

Custom Binds: You can rebind the profile switcher to any key in the menu.

</details>

<details>
<summary><b>🛠 Known Issues & Roadmap</b></summary>

Bug: Burst Mode is currently broken. Keep the burst checkbox off or the pull logic will fail.

Future: Working on an Arduino-based hardware bypass for better security.

</details>
