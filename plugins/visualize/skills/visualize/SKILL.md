---
name: fullish-visualize
description: Draw architecture diagrams, flowcharts and data charts for QQ directly in the Dim plugin.
---

Use `visualize_guide` to read the overview and the relevant guide on demand. Choose SVG for
precise figures, Mermaid for relational diagrams, and HTML/CSS for small explanatory cards.
Create a bounded source and call `visualize_render`. Inspect the actual returned image,
including Chinese labels, clipping, contrast and data accuracy. If needed, revise once.
Register the chosen `artifact_id` and a natural caption with `fullish_reply`, then finish.
Guides and tools are available only when the host grants this group permission. Never invent
an artifact ID, claim that an unrendered source is an image, or send intermediate instructions.
