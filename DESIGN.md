---
version: alpha
name: PDF2MD
description: "A quiet document workbench for turning PDFs into readable Markdown."
colors:
  primary: "#176b60"
  background: "#eef2f4"
  surface: "#ffffff"
  ink: "#172d38"
  muted: "#526873"
  line: "#d7e1e6"
  accent: "#e8f3ee"
  warning: "#805116"
  danger: "#a83240"
  focus: "#087e8b"
typography:
  sans:
    fontFamily: "Segoe UI, PingFang SC, Microsoft YaHei, system-ui, sans-serif"
  mono:
    fontFamily: "Cascadia Code, SFMono-Regular, Consolas, monospace"
rounded:
  DEFAULT: "10px"
  sm: "6px"
  lg: "16px"
spacing:
  unit: "6px"
  section: "24px"
components:
  button:
    height: "38px"
  panel:
    backgroundColor: "{colors.surface}"
---

# PDF2MD Design System

## Overview

This is a desktop document workbench, informed by a typesetter's proof desk: a task
ledger on the left and a generous paper reading surface on the right. The signature
is the small PDF → MD conversion mark and a numbered document queue. The main job
is to select, convert, compare against the source and save reviewed results.

Requirements are in REQUIREMENTS.md v0.4 (application 0.2.1). Simplified Chinese is the product locale.
Windows and macOS share behavior; native file dialogs and native select popups may
follow OS conventions. No Japan-specific market assumptions apply. There is one main
conversion workspace, a settings panel and a source/Markdown review workspace sharing
the same controls and paper treatment.

Token ownership: this frontmatter generates `frontend/tokens.css` through
`scripts/design_tokens.py`. `npm run tokens:check` detects drift. Runtime components
only consume those semantic CSS variables. Components and behavior live in the shared
`frontend/app.ts`, with canonical choices recorded in UX-CONTRACT.md.

## Colors

Cool slate surfaces separate controls from white document paper. Deep pine is reserved
for the main action, selection and completion. Amber means review is needed; rose means
failure. Every status also has a text label and icon. A light theme is the initial scope.

## Typography

System fonts avoid downloads and support Chinese on both platforms. The brand and document
heading use a restrained bold sans; body text uses generous line height. Technical paths,
page counts and Markdown source use the mono family. No external fonts are fetched.

## Layout

The desktop has a 76px header, a 390px queue/settings region and flexible preview paper.
The queue and preview each own their bounded scrolling; settings are a natural-height
disclosure. Below 880px, panels stack with document scrolling. At short heights all
controls remain reachable; status and action geometry do not jump during progress.

Review mode replaces conversion settings with a bounded issue ledger and two document
surfaces: the original PDF in the center, Markdown source and live preview on the right.
Source-region overlays use the existing warning/focus colors and a visible issue context;
they must align with the original page at every zoom. At narrow widths the issue ledger
can collapse and the two document surfaces stack. Each paper/editor scrolls independently,
and navigation remains reachable without shrinking the document to unreadable text.

## Elevation & Depth

Borders and tonal surfaces define hierarchy. Only menus, notifications and dialogs use
subtle elevation. No gradients, translucent glass or dashboard card mosaics.

## Shapes

10px controls, 16px panels, 6px tags; status tags are compact rounded rectangles.
Lucide icons use consistent 18px strokes, alongside visible labels for core actions.

## Components

Buttons share brand, outline and ghost variants, stable busy dimensions, visible focus,
disabled and hover states. Native selects own popup geometry. A shared toast live region
handles short feedback; actionable errors stay inline. Native HTML dialog owns modal focus
and inert background for exit confirmation. Reduced motion removes transitions.

Scrollbars use global standards properties and WebKit fallbacks. Ordinary conversion
preview remains read-only. The review workspace owns one CodeMirror 6 Markdown editor
with the system mono family, the shared focus treatment and keyboard save shortcut.
Its source and rendered preview are linked to the same issue/block selection. Avoid
rebuilding editor keyboard, composition, history or syntax behavior in local controls.
Read-only textareas retain `resize: none`. Unsaved-change and conflict dialogs follow the
existing native HTML dialog focus and inert-background contract; keep drafts on failure.
All asynchronous status is factual; unknown work uses an indeterminate indicator.

## Do's and Don'ts

- Keep the primary action named “开始转换” and retain queue selection during updates.
- Keep files local, source paths available in details, and results easy to open.
- Preserve all conversion warnings after review; distinguish pending, reviewed and saved.
- Show a page-level location when exact source or text mapping is unavailable.
- Never show placeholder sample results as actual conversions.
- Never expose engine configuration names in normal user flows.
