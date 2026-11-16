# Modern web guidance: guide index

Every guide id with its one-line description, by category. Inside Faustus read one with the `web_guidance` tool (`action=retrieve`). Elsewhere, read it at `https://raw.githubusercontent.com/GoogleChrome/modern-web-guidance/main/skills/modern-web-guidance/guides/<category>/<id>.md`.

## accessibility

- `accessibility`: Actionable coding guidelines for building accessible web applications, covering semantic HTML, focus management, forms, media, and testing. Use this skill when auditing or implementing accessibility features, keyboard navigation, or ARIA.
- `accessible-error-announcement`: Synchronize programmatic accessibility states (like aria-invalid) with the visual :user-invalid state to ensure screen reader users receive error feedback only after interaction, mirroring the visual experience.
- `accessible-generated-content`: Use CSS generated content (`::before`/`::after`) in ways that stay accessible, including alternative text for decorative content.
- `respect-os-text-scale`: Respect the operating system's text scale preference for a page so font sizes, layout, and rem-based measurements follow the user's accessibility settings.

## built-in-ai

- `language-detection`: Detect the language of user-generated content or already present site content.
- `language-model`: Run on-device natural language inference in the browser using the Prompt API, with streaming output, structured JSON responses, and multi-turn session management.
- `prompt-api`: The Prompt API allows developers to run natural language processing tasks directly in the browser using **Gemini Nano**. This built-in AI approach ensures user privacy, reduces server costs, and enables offline functionality.
- `summarizer`: Summarize text content using the on-device Summarizer API.
- `translator`: Translate text between languages using the on-device Translator API.

## css

- `animate-to-intrinsic-sizes`: Smoothly animate interactive components (like accordions, menus, and expanding cards) to and from their natural dimensions.
- `atrule-support-conditionals`: Conditionally apply certain CSS rules or declarations only if a certain at-rule is supported by the browser.
- `calculate-with-intrinsic-sizes`: Calculate the size of an element based on its intrinsic size, while ensuring it fits within given design constraints.
- `child-state-based-styling`: Build a component that changes its styling based on the state of one of its child elements. For example, a component that renders in light or dark mode based on whether a theme toggle is checked (or not).
- `content-based-styling`: Build a component that changes its layout based on whether it contains specific child elements (or not). For example, if the component contains an image, use a multi-column layout, otherwise default to a single-column layout.
- `css`: Action-oriented guidelines for modern CSS architecture, including the cascade, selectors, scoping, nesting, and design tokens. Use this guide when authoring styles or managing design systems.
- `css-layout`: Modern CSS layouts such as flexbox, grid, subgrid, container queries, anchor positioning, and intrinsic sizing. Use this skill when architecting responsive UI components or page layouts.
- `design-token-reactivity`: Define higher-order design tokens, like density modes (compact, comfortable, spacious) or themes and have descendant components react to changes directly and in component-appropriate ways.
- `dynamic-sibling-styling`: Create dynamic visual spectrums or layout arrangements that automatically adapt to the number of elements in a group.
- `fluid-scaling`: Scale items like font size, spacing, and media sizes smoothly based on the parent container's size rather than using fixed breakpoints
- `individual-transform-properties`: Animate or override individual CSS transform properties (e.g. translate, rotate, scale) independently of other transform properties on a single element.
- `overflow-clipping-control`: Adjust the visible clipping boundary of an element to align with the content edge, padding edge, or border edge—or a specified offset from any of these—offering finer-grained control over how content is clipped.
- `reduce-style-repetition`: Reduce excessive style repetition by encapsulating complex or dynamic styling logic into reusable functions (such as a function that computes a gradient based on a set of input parameters).
- `responsive-design`: Build UI that adapts to container and viewport size, including container queries, dynamic viewport units, aspect ratios, and responsive typography.
- `selector-atrule-combinations`: Target elements based on the intersection or union of selectors and conditional @-rules while minimizing repetition.
- `size-aware-styling`: Build a component whose styles can be conditionally dependent on its own width or height, rather than the width or height of the viewport. For example a card component that can change its layouts depending on how large it is, or a call-to-action button that can conditionally display helper text based on its width.
- `style-parent-with-has`: Style parent elements of a form field (e.g. labels or fieldsets) when the field is invalid.
- `usage-aware-component-variations`: Build components that adapt visual logic based on semantic context using CSS container style queries.

## forms

- `animated-select-picker`: Create a custom select component whose dropdown is animated. For example, the menu fades or slides into view, or the options animate upon selection.
- `autofill-address-form`: Build an address form with correct autocomplete attributes and autofill support.
- `autofill-highlight-inputs`: Customize the styling of form fields that have been autofilled and not edited by the user
- `autofill-payment-form`: Build a payment form that collects card details with correct autocomplete values and autofill support.
- `autofill-sign-in-form`: Build a sign-in form with correct autocomplete values and autofill support.
- `autofill-sign-up-form`: Build a sign-up form with correct autocomplete values and autofill support.
- `brand-consistent-forms`: Match checkboxes, radio buttons, range sliders, and progress bars to your site's color palette without replacing them with custom components.
- `branded-select-styling`: Create custom select elements whose button, picker, arrow icon, and checkmark all seamlessly match your brand or design system's typography, colors, spacing, and border treatments.
- `custom-select-picker-layouts`: Create custom select pickers whose options are positioned in unique or interesting ways, rather than the traditional stacked list of options.
- `form-fields-automatically-fit-contents`: Allow form fields to grow and shrink to fit the user input, e.g. as the user types or selects a different option. Apply maximum and minimum size limits to create dynamic and responsive form fields that conform with the page design.
- `forms`: Best practices for building accessible, secure, and user-friendly web forms. Use this guide when creating or modifying forms, inputs, and submission flows.
- `ime-safe-enter-submit`: Implement Enter-to-submit in textareas and single-line inputs (chat, search, inline rename) safely for IME (Input Method Editor) users to prevent submitting incomplete text.
- `required-field-feedback`: Provide error message for required form fields that were skipped or left empty *only* after user interaction, to avoid preemptive errors and ensure feedback is timely and contextually relevant to the user's flow.
- `rich-media-picker`: Create a custom select component whose options can contain complex HTML formatting (e.g. images, icons, and other rich formatting) rather than just plain text.
- `select-menu-interaction`: Validate that a non-default option has been chosen in a select menu only after the user has interacted with the control.
- `slider`: Style a native range slider with custom track, thumb, fill, and focus states while preserving native functionality and accessibility.
- `sms-otp-form`: Verify user phone numbers and one-time passcodes delivered over SMS with automatic code extraction and accessible form input.
- `switch`: Build an accessible switch with the native HTML switch attribute, falling back to a styled checkbox when not supported.
- `validate-input-after-interaction`: Show form field validation feedback (e.g. password complexity or email format requirements) only after the user has finished their initial interaction, avoiding premature errors on page load or while the user is typing.

## html

- `accessible-web-components`: Make custom elements accessible by default, setting roles and ARIA state via the ElementInternals ARIA mixin, managing focus with delegatesFocus, and keeping ARIA relationships within a single shadow root.
- `custom-elements`: Define, register, and manage the lifecycle of autonomous custom elements, covering constructor rules, lifecycle callbacks, upgrade timing, attribute/property reflection, and naming conventions.
- `form-associated-custom-elements`: Make a custom control participate natively in forms with formAssociated and ElementInternals, submitting its own value, integrating with constraint validation, and reacting to reset, disable, and state-restore.
- `html`: Action-oriented guidelines for modern HTML architecture, semantics, native interactive APIs (Dialog, Popover, Details), focus management, and resource prioritization. Use this guide when structuring web documents, implementing native overlays, or optimizing resource loading order.
- `prerendering-custom-elements`: Server-render a custom element, then hydrate it in place without re-rendering or a flash of unstyled content.
- `shadow-dom`: Encapsulate a component's DOM and styles with Shadow DOM, construct the tree efficiently with HTML templates, and project consumer content with slots.
- `styling-web-components`: Style across the shadow boundary with :host selectors, inherited and custom properties for theming, ::slotted and ::part, and cascade layers and container queries inside components.
- `web-components`: Orientation and cross-cutting principles for authoring and using Web Components, covering Custom Elements, Shadow DOM, templates and slots, Declarative Shadow DOM, styling across the shadow boundary, Form-Associated Custom Elements, and accessibility requirements. Use this guide when building reusable UI components, design systems, or server-rendered custom elements.

## js

- `calculate-event-differentials`: Calculate elapsed or remaining time between dates and times, balance duration units, and format human-readable localized durations using Temporal.Duration and Intl.DurationFormat.
- `capture-location-agnostic-data`: Model location-agnostic calendar dates, wall-clock times, partial dates (recurring birthdays, credit card expirations), and recurring monthly intervals without time zone drift using Temporal Plain types.
- `coordinate-global-events`: Schedule future meetings or events by explicitly binding them to a geographical IANA time zone so that event times remain accurate regardless of Daylight Saving Time (DST) transitions, "skipped" or "repeated" hours during clock changes.
- `js`: Action-oriented guidelines for modern JavaScript and Web APIs, covering immutable data operations, deep cloning, collection grouping, asynchronous coordination, URL query parsing, binary text encoding, and DOM manipulation. Use this guide when authoring or refactoring client- or server-side JavaScript.
- `sort-high-precision-timestamps`: Parse, sort, and measure intervals between sub-millisecond or nanosecond ISO 8601 timestamps without Date truncation collisions using Temporal.Instant.
- `support-global-calendar-systems`: Display and calculate dates in non-Gregorian calendar systems (e.g., Islamic, Hebrew, or Chinese) accurately for international users.

## performance

- `batch-analytics-events`: Debounce and batch multiple analytics, telemetry, and Core Web Vitals events together in a single beacon to minimize network contention and reduce server load, while still delivering real-time updates.
- `break-up-long-tasks`: Break up heavy synchronous processing (complex computations and/or long loops) or DOM updates to improve Interaction to Next Paint (INP) responsiveness and let the browser handle user input and repaint the screen.
- `calculate-total-foreground-time`: Calculate the total time a user actually spent viewing a page, excluding periods when the tab was in the background.
- `conditional-async-dependencies`: Conditionally load or initialize async dependencies (such as importing polyfills for missing web features) without requiring complex orchestration across all of a page's script dependencies.
- `defer-rendering-heavy-content`: Reduce rendering times in content-heavy web pages (e.g. pages with long feeds, lots of articles, or complex dashboards), by deferring rendering for any content that is not immediately visible to the user.
- `defer-work-until-scroll-ends`: Defer expensive operations like DOM updates, data fetching, analytics tracking, or layout recalculation until after scrolling completes to maintain smooth scroll performance.
- `deliver-optimized-decorative-images`: Deliver optimized decorative images (such as backgrounds, UI icons, or complex masks) by simultaneously providing next-generation image formats (like AVIF or WebP) alongside multiple pixel densities (like 1x and 2x) so the browser can dynamically negotiate the best combination of file size and visual quality for the user's device capabilities.
- `deprioritize-background-fetches`: Deprioritize background data fetches made with the Fetch API to prevent network contention with user-initiated requests.
- `detect-initial-visibility-state`: Reliably determine whether a page was initially loaded in the background, even in cases where the script is loaded asynchronously after the user foregrounded the page.
- `efficient-background-processing`: Conserve system resources and battery life by pausing background JavaScript execution (such as `<canvas>` animations, WebGL rendering, or high-frequency WebSocket data polling) when the component is off-screen and then resume them just-in-time when they scroll back into view.
- `faster-spa-view-transitions`: Enable faster transitions back to previously visited views in a Single-Page Application (SPA) by preserving their structural DOM state instead of destroying and rebuilding them on every navigation.
- `flicker-free-client-side-ab-testing`: Deliver and render A/B tests, multi-variate tests, or other experiments using client-side JavaScript to alter or inject HTML, CSS, and JavaScript without the original content showing first before flickering or flashing to show the experiment content.
- `full-session-analytics`: Reliably track analytics, Core Web Vitals, errors, and telemetry data across the user's entire page visit, and defer sending of the data until the user leaves the page.
- `identify-heavy-scripts`: Identify the scripts most responsible for long animation frames
- `identify-inp-causes`: Identify slow-running JavaScript and other causes of poor Interaction to Next Paint (INP) responsiveness.
- `improve-next-page-load-performance`: Improve page load performance by prefetching or prerendering pages that the user is likely to visit next.
- `interaction-paint-timing`: Measure the time it takes for user interactions and client-side navigations to render new contentful updates on screen.
- `interactions-in-complex-layouts`: Make interactions snappier and more responsive (reducing Interaction to Next Paint (INP) scores) by avoiding layout re-calculations in complex layouts, such as data-heavy dashboards or spreadsheet-style grids.
- `lazy-load-video-and-audio`: Defer downloading offscreen video and audio assets, including video poster images and preloaded media data, until the media player approaches the viewport to conserve bandwidth and reduce network contention during initial page load.
- `optimize-image-priority`: Optimize the loading priority of Largest Contentful Paint (LCP) candidate images and deprioritize non-critical images to reduce critical resource load delays.
- `optimize-preload-priority`: Optimize the relative priority of preloaded content to reduce critical resource load delays.
- `optimize-script-priority`: Optimize the loading priority of scripts by boosting critical asynchronous scripts and deprioritizing non-essential or late-body scripts to improve sequencing and reduce delays.
- `out-of-order-html-streaming`: Declaratively update a page with content streamed later into the same HTML document or via imperative JavaScript APIs.
- `performance`: Actionable guidelines for optimizing modern web applications. Use this guide when auditing performance, optimizing loading metrics, fixing slow interactions and optimizing Core Web Vitals (LCP, INP, CLS)
- `resolution-optimized-pseudo-elements`: Use resolution-optimized images in CSS pseudo-elements (such as `::before` and `::after`) to reduce the number of DOM nodes.
- `schedule-tasks-by-priority`: Schedule, reprioritize, or abort queued tasks across priority levels so critical user input runs ahead of background work.

## privacy

- `privacy`: Action-oriented guidelines for web developers to implement privacy by design, data minimization, third-party audits, and secure data handling. Use this skill when designing applications, integrating third-party services, handling user data, or configuring security headers.

## pwa

- `install-web-app`: Install web applications from a page using browser-controlled consent UI.
- `migrate-web-app-origin`: Transfer an installed web app to a new origin while preserving user configuration and OS-level shortcuts.

## security

- `client-side-encryption`: Establish shared secrets using public-key encapsulation or key agreement—including hybrid and post-quantum protection—and encrypt sensitive data in the browser with authenticated encryption.
- `digital-signatures`: Sign and verify messages, tokens, or artifacts in the browser using classical or quantum-resistant (post-quantum) asymmetric keys to ensure data integrity and authenticity.
- `local-network-access`: Access resources on a user's local network or loopback interface from a public web application.
- `passkey-authentication`: Authenticate a returning user with a passkey for primary sign-in.
- `passkey-conditional-create`: Silently register a passkey for an existing user after a successful password login.
- `passkey-management`: Let users view and manage the passkeys registered to their account.
- `passkey-reauthentication`: Verify a signed-in user's identity using their existing passkeys before a sensitive action.
- `passkey-registration`: Register a passkey for an existing user account.
- `passkeys`: Comprehensive orientation and cross-cutting principles for implementing WebAuthn and Passkeys in web applications. Use this guide when handling passkey registration, authentication, management, or reauthentication.
- `quick-sign-ins`: Authenticate returning users in place with their saved passkeys or passwords before navigating to a sign-in page or interrupting their workflow.
- `restrict-outbound-connections`: Restrict a page or worker to communicating only with an explicit allowlist of network endpoints to prevent unauthorized third-party requests and client-side data exfiltration.
- `sanitize-untrusted-html`: Safely parse and display untrusted HTML content from user input by removing unsafe elements and attributes.
- `secure-external-subresources`: Verify the cryptographic integrity of external scripts, stylesheets, fonts, and images loaded from third-party origins, and control whether those requests send credentials or referrer URLs.
- `security`: Preventative security guidelines for web developers (XSS, CSP, Cookies, Cross-Origin Isolation). Use this skill to guide the process of auditing, testing, and deploying security policies safely.
- `trusted-types`: Prevent DOM-based XSS attacks by defining a Trusted Types policy to sanitize untrusted HTML, using the policy when writing to DOM sinks like innerHTML, and enforcing Trusted Types via Content Security Policy.
- `validate-origins`: Compare and validate web origins and sites safely across cross-document messages and URLs without error-prone string parsing.
- `verify-email-ownership`: Verify that a user owns an email address during account creation, sign-in, or recovery without sending a one-time passcode or magic link email.

## ui-atoms

- `carousel-slide-effects`: Create a carousel of slides with images or other visual elements, where each slide animates as they enter/center/exit their scroller. For example, the slides may fade-in/fade-out, rotate, get bigger or smaller, etc.
- `component-specific-light-dark-theme`: Force certain elements to be in light mode or dark mode (e.g. code blocks, media players, etc) independently of the page's color-scheme.
- `icons`: Display and manage icons that can be set and parameterized from CSS, are fast to load, crisp at any resolution and are properly exposed to (or hidden from) assistive technologies.
- `position-aware-tooltips`: Build tooltips and popovers with directional arrows (or other visual styling) that automatically point the correct way when the element flips to a fallback position.
- `pull-to-reveal`: Build a pull-to-reveal feature that would enable the user to pull down on the screen to reveal more content, like a search bar.
- `responsive-table`: Build a data table that presents tabular data with proper header semantics, and remains readable and navigable on small screens and with assistive technologies.
- `scroll-position-aware-elements`: Build floating buttons or widgets (back-to-top, scroll-to-bottom, chat launchers, etc.) that appear and disappear based on whether the user has scrolled at all.
- `scroll-progress-indicator`: Create a scroll progress bar, stepped progress tracker, or any visual affordance that communicates how far through a page or section the user has scrolled.
- `scrollability-affordance-hints`: Build scroll shadows or fading edges that appear only when there's actually more content to scroll to in that direction.
- `shrinking-header-on-scroll`: Smoothly animate a fixed header or full-page cover on scroll to dynamically shrink, gain shadows, and transform its layout over a predefined scroll distance.
- `single-line-overflow`: Show text that must stay on one line, such as a tab label, table cell, navigation item, or file name, in a space it may not fit, without breaking the layout or losing the text.
- `state-aware-sticky-headers`: Build sticky section headers or navbars that visually transform when they're actually "stuck" at the top, changing their color scheme or gaining a shadow.
- `viewport-aware-dropdowns`: Position dropdowns, flyouts, or contextual action panels tethered to a trigger button so they automatically flip axes at viewport edges instead of getting clipped.

## ui-behaviors

- `2d-panning`: Allow users to freely pan or scroll two-dimensional surfaces—such as maps, zoomed images, infinite canvases, or large data grids—across both directions (whether diagonally or changing direction mid-gesture) without the gesture locking to a single horizontal or vertical axis.
- `anchor-positioning-tab-underline`: Transition an element seamlessly between two target element positions. For example, moving a selected tab underline between the previously selected tab and the currently selected tab.
- `animate-element-entry-exit`: Smoothly hide/show elements as they are added/removed from the DOM or as their display values are toggled.
- `animate-to-from-top-layer`: Animate elements such as dialogs, popovers, and tooltips as they're entering/exiting the top layer.
- `carousel-snap-highlights`: Visually highlight the currently snapped non-interactive item in scroll-snapping carousels, galleries, or full-page swipe experiences. For example, expanding a card when snapped, or revealing hidden content.
- `consistent-cross-document-transitions`: Ensure critical page state is loaded and stable before initiating a cross-document view transition. This means critical CSS styles are loaded and applied, critical JavaScript is loaded and run, and the HTML visible for the user's initial view of the page has been parsed before the transition runs.
- `cross-document-transitions`: Create smooth, seamless transitions between full page navigations, such as cross-fades, custom reveal effects, or morphing of content from one page to the next.
- `custom-button-actions`: Declaratively connect a button to any element to trigger custom, application-specific actions using declarative button commands, invoker commands, button commands, custom commands, or declarative toggle actions.
- `declarative-dialog-popover-control`: Toggle the visibility of a dialog or popover from a button without writing JavaScript.
- `directional-navigation-transitions`: Animate visual state changes to reflect the direction of a user's navigational flow, such as sliding new content in from the right when advancing forward or from the left when returning to a previous screen.
- `drag`: Support dragging to move a floating element around the page.
- `drag-and-drop`: Support dragging to rearrange the children of a container along one axis (horizontal or vertical) while preserving their state and accessible order.
- `dynamic-sibling-animations`: Stagger animation or transition timing across sibling elements so each one starts after a computed delay based on its position in the sibling list.
- `dynamic-sort`: Best practices for handling dynamic user-triggered reordering of a container's children (e.g. list items, table rows, or a card grid), with optional animated reordering that respects user preferences, good screen reader UX, and optimal performance for large lists.
- `group-element-transitions`: Smoothly rearrange and animate list items, cards, or grid elements into their new positions simultaneously when adding, removing, shuffling, or reordering—such as removing a product from a shopping cart.
- `highlight-text-ranges`: Highlight arbitrary text ranges on a page such as search results, spelling errors, or collaborative editing cursors.
- `interactive-content-reveal`: Create interactive reveal effects, such as a spotlight that follows the user's pointer to uncover details within an image or UI section.
- `interest-triggered-action-previews`: Show a live preview of a button's effect when a user signals interest (e.g. hovering, focusing, or long-pressing) but before they commit to clicking.
- `interest-triggered-tooltips`: Show a tooltip or supplemental information when a user hovers over, focuses on, or long-presses an interactive element, without requiring a click.
- `light-dismiss-a-dialog`: Create a modal dialog that can be closed via light dismiss (i.e. clicking or tapping outside of the dialog)
- `motion`: Animate UI with CSS transitions, animations, scroll-driven animations, and view transitions while keeping motion on the compositor and respecting reduced-motion preferences.
- `move-dom-element-without-losing-state`: Move or reparent a DOM element without losing important element state, such as interactivity states (:focus/:active), `<iframe>` loading state, animation/transition state, etc
- `parallax-scroll-effects`: Create scroll-based effects (such as parallax) where foreground and background layers move at different rates, creating a sense of depth as the user scrolls.
- `persistent-top-layer-ui`: Keep a modal dialog, fullscreen element, or native popover visibly open and functionally active when its underlying DOM node is moved or reparented in the DOM.
- `physics-based-easing`: Create custom, physics-based animation and transition effects, like bounce and spring, that feel more natural and engaging than traditional easing curves.
- `platform-controls-dismiss-dialog`: Create a modal dialog that can be closed via standard platform-specific user actions, such as pressing the `Esc` key on desktop platforms, or a "back" or "dismiss" gesture on mobile platforms
- `prevent-page-scroll-under-modal-dialog`: Prevent the underlying page from scrolling while a modal dialog is open.
- `same-document-transitions`: Visually connect persisting elements across different page states or navigations in a Single Page Application (SPA) (e.g. expanding a product thumbnail into a full-bleed hero image) by smoothly morphing their size, position, or other styling properties.
- `scoped-component-transitions`: Animate state changes inside a single component (a card, list, panel, or other subtree) while the rest of the page remains interactive, and other transitions run concurrently.
- `scroll-completion-effects`: Trigger visual effects, animations, or focus changes on a target element only after a smooth programmatic scroll to that element has finished.
- `scroll-entry-exit-effects`: Create fade-in, scale-up, or other complex reveal-type effects on elements as they enter and exit the scrollport (or viewport) while the user is scrolling.
- `scroll-snap-realtime-feedback`: Provide real-time visual feedback in linked UI elements while a user scrolls through snap-aligned content, before the scroll gesture completes.
- `scroll-snap-state-sync`: Synchronize navigation indicators, linked content panels, and analytics tracking with the actively snapped item in a scrollable container.
- `scroll-target-on-load`: Build a scrollable list of elements (e.g. a carousel of images or a chat conversation thread) that can be displayed with a particular element scrolled into view on the initial render.
- `scrollytelling`: Animate visual properties on a target element — such as fading a backdrop, shifting a background color, or to create scrollytelling experiences — driven entirely by the scrollport position of a completely different element.
- `search-hidden-content`: Hide content from view using patterns such as accordions, tabs, and "Read more" sections, while ensuring the hidden text reveals itself during native "Find in page" searches, allows search engine indexing, supports URL fragment deep links, and maintains ARIA accessibility.
- `sortable-table`: Progressively enhance a data table with behaviors that allow users to sort rows by clicking on column headers.
- `spatial-navigation`: Implement accessible spatial navigation using arrow keys that works correctly in different layout modes (grid etc)
- `swipe-to-remove`: Let users act on items in a list (remove, archive, mark as read, etc.) with a horizontal swipe gesture, so they can process entries quickly without tapping a separate control.
- `swipe-to-reveal`: Let users swipe horizontally on a list item or content pane to reveal contextual action buttons or a side menu without automatically triggering an action.

## ui-components

- `accordion`: Build an accordion component with animation, optional exclusive (one-open-at-a-time) behavior, and brand-consistent styling.
- `app-menus`: Build an application menu bar where several top-level menus (such as File, Edit, and View) act as a single keyboard stop and switch between each other on hover or arrow keys.
- `breadcrumbs`: Build a breadcrumb trail that shows the user's location within a site hierarchy and lets them navigate to ancestor pages.
- `card`: Build a card component that adapts its presentation to its own available space and contents.
- `carousel`: Build a carousel component that displays a set of items in a horizontal or vertical layout.
- `checkbox`: Style checkboxes, including custom icons, layout, colors, check/uncheck animations, while preserving native functionality and accessibility.
- `copy-button`: Build a copy button that copies text from arbitrary elements to the clipboard.
- `dark-mode-toggle`: Build a dark mode toggle component that allows users to override their system color scheme preference for the current site.
- `dialog`: Build a modal or non-modal dialog component that is accessible, manages focus appropriately, and can be dismissed the way users expect on each platform.
- `hovercard`: Build a hovercard component that displays an interactive structured preview of the page a certain type of link (e.g. a user profile) will navigate to when hovered over.
- `menu`: Build a menu component that displays a list of options or actions, with support for nested submenus.
- `navbar`: Build a site navigation bar that adapts across screen sizes and indicates the current page.
- `navigation-drawer`: Create a navigation drawer component that, when triggered from a menu button, slides in from the side overlaid on top of existing page content, and slides out when dismissed (by swiping away, tapping outside, or pressing escape).
- `persistent-app-tours`: Create persistent onboarding walkthroughs using tethered native overlays that stay open during user interaction.
- `persistent-toast-notifications`: Create non-intrusive toast and overlay notifications for persistent, stackable messaging and state communication.
- `progress`: Build a styled progress bar that communicates completion of a task or process, in both determinate and indeterminate states.
- `progress-ring`: Build a progress ring component that visually represents the completion status of a task or process, with support for content in the center and brand-consistent styling.
- `scrollspy`: Highlight the currently visible section of a page in a navigation menu
- `spinner`: Build a loading spinner that communicates busy state to all users, respects reduced-motion preferences, and animates efficiently.
- `stack-drill-down`: Build full-screen hierarchical navigation that lets users drill down into nested views and swipe or navigate back to return, with browser history kept in sync.
- `tabs`: Build a responsive tabbed interface with good UX when not all tabs fit on screen.
- `toolbar`: Build a toolbar component that displays a set of controls for performing actions.

## user-experience

- `content-first-noise-reduction`: Maximize content and reduce noise by removing blocking popups, keeping persistent navigation compact, and moving secondary content behind on-demand disclosure.
- `form-factor-adaptation`: Adapt layouts, controls, and text to any device or container so touch targets stay usable, content fits its space, and nothing collides with system UI.
- `natural-interactions`: Implement natural interactions with clear hover, focus, and press feedback, motion that opens from its trigger, swipeable content, and keyboard-reachable actions.

## visual-design

- `adapt-scrollbar-to-contrast-preferences`: Enhance scrollbar visibility for users who prefer high-contrast interfaces
- `apply-webgl-shaders`: Apply custom visual effects with WebGL shaders to HTML content.
- `browser-ui-theming`: Theme browser-generated UI such as text selection, scrollbars, form controls, and pickers with CSS instead of re-creating it with custom elements.
- `color`: Color guidance for the web, including contrast requirements, light and dark color schemes, Forced Colors Mode, and generating tints and shades from a base color.
- `complex-shapes`: Clip elements and their content to any free-form shape, like a symbol, brush stroke, or organic texture for more expressive designs.
- `contrast-color`: Select a readable text color with sufficient contrast on a dynamic background color.
- `custom-text-decorations`: Add text decorations such as underlines, overlines, strikethroughs, or entirely custom shapes, with reveal animations, custom offsets and end insets, and flexible ink skipping.
- `customize-scrollbar-color-and-thickness`: Customize the color or thickness of a scrollbar
- `dark-mode`: Implement dark mode support in a way that respects the user's light/dark theme preference and adapts browser UI (e.g. scrollbars, form controls, etc)
- `equal-width-text-lines`: Make every line of a multi-line heading or lockup span the same width, as in poster-style or stacked-headline typography. Unlike justified text, the text itself is sized to the width rather than the spacing stretched
- `export-html-media-from-canvas`: Capture and export dynamic HTML content as images or video frames from within canvas.
- `expose-canvas-content-to-browser-features`: Expose content rendered in a canvas to browser features like assistive technologies, translation, or reading mode.
- `improve-text-layout-and-legibility`: Improve the layout and legibility of short standalone text content, such as headings no longer than a few lines, by enabling the browser to apply evenly balanced line breaks when wrapping text.
- `interactive-content-in-3d-scenes`: Integrate interactive HTML elements into a 3D scene.
- `precise-text-alignment`: Achieve precise vertical alignment with text of any font. For example, exactly equal visual padding above and below text, or aligning text perfectly flush with adjacent icons or images.
- `shaped-cutouts`: Combine multiple shapes to create complex cutouts or 'knockout' effects in elements, such as adding a notch to an element.
- `soft-edge-content-fade`: Apply a transparency gradient to content edges to indicate further scrollable areas or to obscure payment-walled text.
- `typography`: Typography best practices for the web, including font sizing units, line height, text wrapping and balancing, and fluid type that scales with the viewport.
- `visual-effects`: Visual effects with CSS such as layered shadows, blend modes, gradients with predictable color interpolation, and patterns like stripes, checkerboards, dots, and pie charts built from gradients alone.
- `visually-stable-font-fallbacks`: Define font styles such that text remains readable and visually consistent in the event that there's a swap between the preferred font and one of the fallbacks (or vice versa).
- `visually-stable-mixed-fonts`: Define font styles such that text remains readable and visually consistent in situations where multiple fonts are used to render a single block of text.
- `visually-texture-content`: Apply realistic weathering and texture patterns to elements to give them an organic, aged, or physical material appearance.

## wasm

- `cpp-on-the-web`: Compile C/C++ to WebAssembly using Emscripten

## webmcp

- `agentic-forms`: Expose client-side functionality as tools to AI agents by annotating standard HTML forms with WebMCP attributes.
- `agentic-javascript-tools`: Programmatically register client-side JavaScript functions as tools for AI agents using the WebMCP Imperative API.
- `webmcp`: Use this guide to implement WebMCP in browser applications, exposing client-side functionality as tools to AI agents via declarative or imperative APIs.
