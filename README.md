# AnyTel Network Ops Agent 

An AI-assisted troubleshooting agent for telecom network operations: it takes a customer-reported
fault, retrieves the applicable runbook, works the diagnostic steps against case data, and drafts
the customer response.

Built on Amazon Bedrock AgentCore, Strands Agents, DynamoDB and Streamlit. AnyTel is a fictional
operator and all data is synthetic.

## Why it's shaped this way

**Runbooks are data, not prompt text.** Each runbook in `runbooks/` ships with a
`.metadata.json` sidecar, so retrieval can filter on metadata rather than relying on semantic
similarity alone. Adding or revising a procedure is a content change, not a prompt change — which
matters when the procedures are owned by network operations rather than by whoever wrote the agent.

**Tools arrive over MCP, not as inlined functions.** `mcp_server/` exposes case lookup and a
DynamoDB gateway as MCP servers, so the agent's tool surface is independently deployable and the
same tools can serve other callers.

**There is an explicit content guard.** `agent/content_guard.py` sits between the model and the
customer-facing output. Drafting text that goes to a customer is the highest-risk thing this agent
does, so it gets a check of its own rather than relying on the model behaving.

## Layout

```
agent/
  network_ops_agent.py        orchestration
  case_analyzer.py            interpret the reported fault
  knowledge_base_retriever.py runbook retrieval with metadata filtering
  email_composer.py           draft the customer response
  content_guard.py            checks on customer-facing output
  config.py  models.py
mcp_server/
  case_lookup_server.py       case data as MCP tools
  dynamodb_gateway.py
runbooks/                     5 procedures + metadata sidecars
scripts/                      setup and deployment
tests/                        pytest + hypothesis property-based tests
ui/                           Streamlit interface
```

Runbooks cover fixed-line broadband outage, intermittent degradation, mobile signal and coverage,
planned maintenance impact, and router/CPE configuration.

## Running it

Dependencies are **exactly pinned** in `pyproject.toml` (Python 3.11–3.13):

```bash
uv sync
uv run pytest           # tests use hypothesis, so they generate their own cases
```

See `scripts/` for environment setup and deployment. Requires an AWS account with Amazon Bedrock
model access.

## Limitations

- Synthetic case data and a fictional operator; no integration with a real OSS/BSS, element
  manager or ticketing system.
- Drafted customer emails are drafts. Nothing here should send mail without a human reading it.
- The content guard checks output; it is not a substitute for the guardrails and approval gates a
  production deployment would need.
- Five runbooks is a demonstration of the pattern, not operational coverage.

---

A personal project. Not an official AWS project, and not supported by AWS.
