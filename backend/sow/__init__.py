"""Statement of Work (SOW) export feature.

A one-shot "feature agent" — triggered from the estimate review page — that turns a
completed `DualScenarioEstimate` into a formatted Statement of Work and renders it as an
editable ``.docx``.

The document structure, boilerplate, branding, and voice are NOT hardcoded: they live in a
YAML template-spec config (``sow/templates/default_sow.yaml``) consumed by a ``python-docx``
renderer. The delivering firm's name lives once, in the template's ``branding.company``, and
every reference uses the ``[COMPANY]`` token — so the SOW re-brands by editing one line. Every section is classified along one axis:

* **deterministic** (``source: boilerplate``) — the firm's branding + legal wording, identical
  across SOWs; static text from the config.
* **non-deterministic** (the agent's job):
  - *project-specific* — ``source: llm`` prose + ``source: estimate`` data mapped from the
    envelope (fee table, schedule, resource summary, project-specific assumptions);
  - *client-specific* — placeholder tokens the agent fills from the project brief/estimate
    **when explicitly present**, else left literal. Never invented.

Pieces: ``models`` (template + runtime schemas), ``config`` (YAML loader), ``agent``
(LLM prose + client-fact extraction with a deterministic fallback), ``mapper``
(estimate→section deterministic renderers), ``composer`` (merge + token resolution),
``renderer`` (``.docx`` bytes).
"""
