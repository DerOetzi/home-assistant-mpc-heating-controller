// Styles for the card's shadow root.
//
// Kept apart from the markup so the layout rules are readable as a whole
// instead of buried in a template literal halfway down the card class.
export const CARD_STYLES = `
        :host {
          /* A dashboard that already defines a heating palette wins; the
             defaults keep the card usable everywhere else. */
          --hc-comfort: var(--color-comfort, #e5533d);
          --hc-eco: var(--color-eco, #43a047);
          --hc-boost: var(--color-boost, #ff6d00);
          --hc-frost: var(--color-frost, #33ccff);
        }
        ha-card { overflow: hidden; }
        .header {
          display: flex; align-items: center; gap: 12px;
          padding: 12px 16px; cursor: pointer;
        }
        /* ha-icon is a plain element: the tinted disc is just its own
           background, so no wrapper is needed. color-mix keeps the disc a
           faint version of the same mode colour that fills the glyph. */
        .status-icon {
          --mdc-icon-size: 24px;
          box-sizing: border-box;
          width: 40px; height: 40px; padding: 8px; border-radius: 50%;
          flex: none;
          color: var(--hc-mode-color, var(--state-icon-color));
          background: color-mix(
            in srgb, var(--hc-mode-color, transparent) 20%, transparent
          );
        }
        .titles { flex: 1; min-width: 0; }
        .title { font-size: 1.05rem; font-weight: 500; }
        .subtitle { font-size: 0.85rem; color: var(--secondary-text-color); }
        .window-icon { color: var(--error-color, #db4437); }
        .window-icon[hidden] { display: none; }
        .radiator-alert { color: var(--warning-color, #ffa600); cursor: pointer; }
        .radiator-alert[hidden] { display: none; }
        .chevron { transition: transform 0.2s ease; color: var(--secondary-text-color); }
        .chevron.open { transform: rotate(180deg); }
        /* auto-fit fits as many equal columns as the min width allows, so a
           phone lands on four and a wide card on more — no hand-tuned
           breakpoint, and it never wraps a fourth value onto its own line. */
        .values {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(68px, 1fr));
          gap: 0 12px;
          padding: 0 16px 8px 16px;
        }
        .value {
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          gap: 1px; cursor: pointer; min-width: 0;
          /* Same 44px touch target as the detail rows. */
          min-height: 44px; padding: 0 6px; margin: 0 -6px;
          border-radius: 8px; -webkit-tap-highlight-color: transparent;
        }
        .value:hover { background: var(--secondary-background-color); }
        .value:active { background: var(--divider-color); }
        .value-main {
          display: flex; align-items: center; justify-content: center; gap: 3px; min-width: 0;
        }
        .value ha-icon {
          --mdc-icon-size: 18px; flex: none;
          color: var(--secondary-text-color);
        }
        .value .num { font-size: 1.05rem; font-weight: 500; }
        .value .unit, .value .label {
          font-size: 0.8rem; color: var(--secondary-text-color);
          white-space: nowrap;
        }
        .boost-row { padding: 0 16px 12px 16px; }
        .boost-row:empty { display: none; }
        /* Room-specific comfort conditions (e.g. a desk-plug Homeoffice
           signal): visible without expanding the card, same as boost. */
        .conditions {
          display: flex; flex-direction: column; gap: 8px;
          padding: 0 16px 12px 16px;
        }
        .conditions:empty { display: none; padding: 0; }
        /* Read-only condition indicator: same tile as a header value, just
           tinted with the comfort/eco colour so the state is still visible
           at a glance, without borrowing the segmented/boost control shape
           that would wrongly suggest it can be tapped to change. */
        .value.condition-indicator {
          justify-content: flex-start; gap: 8px;
          background: color-mix(in srgb, var(--seg) 12%, transparent);
          border-radius: 8px; margin: 0; padding: 0 8px;
        }
        .value.condition-indicator ha-icon {
          color: var(--seg);
        }
        .value.condition-indicator .num { color: var(--seg); }
        .value.condition-indicator:hover {
          background: color-mix(in srgb, var(--seg) 22%, transparent);
        }
        .value.condition-indicator:active {
          background: color-mix(in srgb, var(--seg) 32%, transparent);
        }
        .details { border-top: 1px solid var(--divider-color); }
        .section { padding: 8px 16px 12px 16px; }
        .section[hidden] { display: none; }
        .divider {
          font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em;
          color: var(--secondary-text-color); margin: 4px 0 8px 0;
        }
        .rows { display: flex; flex-direction: column; }
        .row {
          display: flex; justify-content: space-between; gap: 8px;
          align-items: center; font-size: 0.95rem;
          /* Comfortable tap target on mobile — the whole row is clickable. */
          min-height: 44px; padding: 4px 8px; margin: 0 -8px;
          border-radius: 8px;
        }
        .row.clickable { cursor: pointer; -webkit-tap-highlight-color: transparent; }
        .row.clickable:hover { background: var(--secondary-background-color); }
        .row.clickable:active { background: var(--divider-color); }
        .row .k {
          color: var(--secondary-text-color);
          display: flex; align-items: center; gap: 8px; min-width: 0;
          /* Long label + long value together can exceed the row's width;
             the label gives way and truncates instead of overlapping v. */
          flex: 1 1 auto; overflow: hidden;
        }
        .row .k ha-icon { --mdc-icon-size: 20px; flex: none; }
        .row .k .label {
          min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .row .v { white-space: nowrap; flex: none; }
        .controls { display: flex; flex-direction: column; gap: 12px; }
        .modes { display: flex; flex-wrap: wrap; gap: 8px; }
        /* One control, not three buttons: a shared outline, hairline dividers
           instead of gaps, and rounded ends only on the outer segments. */
        .segmented {
          display: flex; overflow: hidden;
          border: 1px solid var(--divider-color); border-radius: 18px;
        }
        button.segment {
          flex: 1 1 0; min-width: 0; min-height: 44px;
          display: inline-flex; align-items: center; justify-content: center; gap: 6px;
          border: none; border-left: 1px solid var(--divider-color);
          background: transparent; color: var(--primary-text-color);
          padding: 6px 10px; font: inherit; font-size: 0.85rem; cursor: pointer;
          white-space: nowrap;
        }
        button.segment:first-child { border-left: none; }
        button.segment ha-icon { --mdc-icon-size: 20px; flex: none; }
        button.segment:hover { background: color-mix(in srgb, var(--seg) 22%, transparent); }
        button.segment:active { background: color-mix(in srgb, var(--seg) 34%, transparent); }
        button.segment.active {
          background: linear-gradient(
            135deg,
            color-mix(in srgb, var(--seg) 78%, black) 0%,
            var(--seg) 100%
          );
          color: #fff;
        }
        button.chip {
          display: inline-flex; align-items: center; gap: 6px;
          border: 1px solid var(--divider-color); border-radius: 16px;
          background: var(--card-background-color); color: var(--primary-text-color);
          padding: 6px 12px; font: inherit; font-size: 0.85rem; cursor: pointer;
          min-height: 44px;
        }
        button.chip.active {
          background: var(--primary-color); color: var(--text-primary-color);
          border-color: var(--primary-color);
        }
        /* The boost chip while boost is active: a state indicator, not a
           second exit competing with Entblocken -- full colour, no dimming,
           no pointer cursor to invite a click that would do nothing. */
        button.chip:disabled { cursor: default; opacity: 1; }
        /* Full width: this is the collapsed card's only direct action, and its
           colour names what it does, the same as an active mode segment.
           --seg defaults to boost's own colour, but a read-only condition
           indicator reuses this exact class with --seg set to comfort/eco --
           same "single full-width state pill" language, different colour. */
        button.boost {
          display: flex; width: 100%; min-height: 44px;
          align-items: center; justify-content: center; gap: 6px;
          border: 1px solid var(--seg, var(--hc-boost)); border-radius: 12px;
          background: color-mix(in srgb, var(--seg, var(--hc-boost)) 14%, transparent);
          color: var(--seg, var(--hc-boost)); font: inherit; font-size: 0.9rem; font-weight: 500;
          cursor: pointer;
        }
        button.boost ha-icon { --mdc-icon-size: 20px; }
        button.boost:hover {
          background: color-mix(in srgb, var(--seg, var(--hc-boost)) 28%, transparent);
        }
        button.boost:active {
          background: color-mix(in srgb, var(--seg, var(--hc-boost)) 40%, transparent);
        }
        button.boost.active {
          border-color: transparent;
          background: linear-gradient(
            135deg,
            color-mix(in srgb, var(--seg, var(--hc-boost)) 78%, black) 0%,
            var(--seg, var(--hc-boost)) 100%
          );
          color: #fff;
        }
        button.boost:disabled { cursor: default; opacity: 1; }
        button.unblock {
          flex: none; display: inline-flex; align-items: center; gap: 6px;
          border: 1px solid var(--warning-color, #ffa600); border-radius: 16px;
          background: transparent; color: var(--warning-color, #ffa600);
          padding: 4px 10px; font: inherit; font-size: 0.8rem; cursor: pointer;
        }
        /* display above beats the hidden attribute unless this is spelled out. */
        button.unblock[hidden] { display: none; }
        button.unblock ha-icon { --mdc-icon-size: 18px; }
        button.unblock:hover {
          background: color-mix(in srgb, var(--warning-color, #ffa600) 18%, transparent);
        }
        .setpoints { display: flex; flex-wrap: wrap; gap: 12px; align-items: stretch; }
        /* Comfort: the bar IS the value. The left cap stays filled so icon and
           readout always sit on colour, and dragging starts to the right of it. */
        .slider {
          /* Narrow enough that it still fits next to the stepper on a phone
             card instead of wrapping to its own line. */
          position: relative; flex: 1 1 140px; min-width: 120px; height: 44px;
          border-radius: 12px; overflow: hidden; cursor: ew-resize;
          /* Same tint as a segment's hover state (color-mix at 22%) -- the
             track reads as "comfort, at rest" instead of a generic grey bar. */
          background: color-mix(in srgb, var(--hc-comfort) 22%, transparent);
          touch-action: none; user-select: none;
        }
        .slider-fill {
          position: absolute; inset: 0 auto 0 0;
          background: linear-gradient(
            135deg,
            color-mix(in srgb, var(--hc-comfort) 78%, black) 0%,
            var(--hc-comfort) 100%
          );
        }
        .slider-label {
          position: absolute; inset: 0; display: flex; align-items: center; gap: 8px;
          padding-left: 12px; color: #fff; font-size: 0.95rem; font-weight: 500;
          pointer-events: none;
        }
        .slider-label ha-icon { --mdc-icon-size: 20px; }
        /* Eco: coarse values, so a stepper beats a text field. */
        .stepper {
          flex: 0 0 auto; display: flex; align-items: center; height: 44px;
          border-radius: 12px; overflow: hidden;
          background: color-mix(in srgb, var(--hc-eco) 22%, transparent);
          border: 1px solid color-mix(in srgb, var(--hc-eco) 55%, transparent);
        }
        .stepper button {
          width: 36px; height: 100%; border: none; background: transparent;
          color: var(--hc-eco); font: inherit; font-size: 1.2rem; cursor: pointer;
        }
        .stepper button:hover:not(:disabled) {
          background: color-mix(in srgb, var(--hc-eco) 25%, transparent);
        }
        .stepper button:disabled { opacity: 0.35; cursor: default; }
        .stepper span {
          display: inline-flex; align-items: center; gap: 6px;
          min-width: 62px; justify-content: center;
          color: var(--primary-text-color); font-size: 0.95rem; font-weight: 500;
        }
        .stepper span ha-icon { --mdc-icon-size: 20px; color: var(--hc-eco); }
        /* Verdict colours for the minimum-flow row's value span; the row itself
           is a plain .row (see above). */
        .ok { color: var(--success-color, #43a047); }
        .low { color: var(--warning-color, #ffa600); }
        /* Readable but visibly inactive: the number still matters for the
           trend, it just cannot drive the system right now. */
        .idle { color: var(--secondary-text-color); opacity: 0.6; }
`;

// Used by the standalone error/hint rendering, which replaces the whole
// shadow root and therefore cannot rely on CARD_STYLES being present.
export const NOTICE_STYLES = `
        .error, .hint { padding: 16px; }
        .error { color: var(--error-color, #db4437); }
        .hint { color: var(--secondary-text-color); }
`;
