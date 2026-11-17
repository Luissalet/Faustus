# Pick a component

Choose by what the person needs to do, then read that component's page in the installed docs (`docs/components/<name>.md`) before writing it. If nothing fits, build that one piece from the core behaviours (`definePrimitive`), not from hand-written markup. Condensed from the library's component table (Apache-2.0, see THIRD_PARTY_NOTICES.md).

| The person needs to | Use |
| --- | --- |
| Run an action | Button |
| Turn a setting on or off | Switch |
| Keep a button pressed, or a group of pressed buttons | Toggle |
| Choose one or more independent options | Checkbox |
| Choose one of a few options, all visible | Radio, or Segmented Control for a compact row |
| Choose one or several from a popup list | Select |
| Choose from an always-visible list | Listbox |
| Search a list and choose one or several | Combobox |
| Type free text with suggestions | Autocomplete |
| Search and run commands | Command |
| Mention or tag with @, / or # while typing | Trigger Menu |
| Mix text with tokens for people, files or tags | Token Input |
| Enter text | Input, or Textarea for several lines |
| Enter a number | Number Input (money: `step={0.01}`) |
| Enter a password or a one-time code | Password Input, OTP Input |
| Enter a date or a time | Date Input (Date Picker with a calendar), Time Input |
| Pick dates from a month grid | Calendar |
| Pick a number or a range by dragging | Slider |
| Pick a score or a colour | Rating, Color Picker |
| Choose, drop or paste files; crop an image | File Input, Image Cropper |
| Label a control with a description and errors | Field, with Label |
| Confirm before an action | Alert Dialog (`AlertDialogClose value="confirm"` is the agreeing answer) |
| Do a focused task over the page | Dialog, or Drawer from an edge |
| Edit or explore something beside its trigger | Popover |
| Choose actions from a button or a right click; an application menu bar | Menu; Menubar |
| Group buttons and toggles with one tab stop | Toolbar |
| Name a control or add a short hint; preview a link | Tooltip; Hover Card |
| Site navigation with panels; switch between panels | Navigation Menu; Tabs |
| Show and hide sections; browse a hierarchy | Accordion (Collapsible for one); Tree |
| Pick from a grid such as seats | Your own component on `createGridFocus` |
| Drag to reorder a list, or cards between columns | Your own component on `createReorder` or `createTransfer` |
| Show a trace or request timings on a time axis | Waterfall |
| Page through results or slides; resizable panels; styled scrolling | Pagination, Carousel; Resizable; Scroll Area |
| Notifications; progress of a task; a measurement in a range | Toast; Progress; Meter |
| A user's image with a fallback; divide content | Avatar; Separator |

Framework notes that change the code you write:

| Framework | Controlled value | Change callback |
| --- | --- | --- |
| React | `value={value} onValueChange={setValue}` | `onValueChange(value, details)` |
| Vue | `v-model="value"` (`v-model:open` for another key) | `@value-change="(value, details) => ..."` |
| Svelte | `bind:value` on `$state` | `onValueChange(value, details)` |
| Solid | `value={value()} onValueChange={setValue}` | `onValueChange(value, details)` |
| Web components | `element.value = ...` | `valuechange` event, `event.detail` |

Popups get `data-starting-style` before they enter and `data-ending-style` while they exit and stay mounted until the exit finishes: put exit styles on `data-ending-style`. They expose `--anchor-width`, `--available-height` and `--transform-origin`. Tailwind targets state as `data-pressed:bg-neutral-100`; plain CSS as `[data-slot="select-trigger"][data-open]`; read the rendered DOM for the exact `data-slot` before writing a selector, since a few differ.
