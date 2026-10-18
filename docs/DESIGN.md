# Interface design

hyprcontroller uses a compact window picker for gamepad navigation on desktop displays and phone streams. The picker dims the desktop and shows each window once.

## Layout

- One vertical row per window keeps selection in a single list. A warm fill and amber outline mark the selected row; keyboard focus adds a thicker outline.
- Workspace numbers sit above the list. An underline identifies the current workspace independently of the selected window.
- The panel is at most 760 logical pixels wide and 560 high. Its height follows the window count until the list needs to scroll. Short and narrow screens reduce spacing and type size.
- Application icons and window titles identify each row. Desktop entries supply readable app names; an outlined window icon covers missing artwork.
- Activation instructions and the controller name share the footer on wide panels and stack on narrow ones. Long controller names use an ellipsis.

## Appearance

Warm charcoal surfaces and off-white text keep the list readable over different desktops. A muted amber accent identifies selection. The panel is opaque; a separate backdrop dims the desktop by roughly one third. The app's layer rule disables inherited blur and X-ray so windows remain visible behind it.

The interface uses the desktop's default sans-serif font and shows state changes directly. Existing rows stay visible during refresh. Empty workspaces explain how to switch, and errors appear as notices.

## Controls

Xbox face-button hints use green A, red B, blue X, and yellow Y circles. Shoulder buttons use their LB/RB labels. These colors identify physical controls.

[ui/ControllerLayout.js](../ui/ControllerLayout.js) defines labels, colors, and shapes by action name. [ControllerButton.qml](../ui/ControllerButton.qml) draws them, and [ControllerHint.qml](../ui/ControllerHint.qml) adds captions. Another layout can replace the display hints without changing backend commands; there is no automatic controller-brand detection.

The application resolves icons from installed desktop entries. It does not bundle application icon files; screenshots may include the installed apps' icons.
