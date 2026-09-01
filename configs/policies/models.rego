# Whether a request may be sent to a given model backend.
#
# The invariant this package states is the one the whole architecture rests on: **C3 and
# C4 never reach an external model.** It is enforced in code at a single point
# (`gateway/model_policy.py`) precisely so it cannot be spread thin; this policy is the
# same rule written where an auditor can read it, and the two are tested against one
# shared table of cases.
#
# Note what is *not* here: no exception, no override attribute, no "unless the tenant
# opted in". A rule with an escape hatch is a rule that will be escaped.
package peak.models

import data.peak.common

default decision := {
	"allow": false,
	"reasons": ["default deny"],
	"obligations": [],
}

decision := {
	"allow": count(denials) == 0,
	"reasons": reasons_for(denials),
	"obligations": obligations_for(input),
	"ceiling": input.ceiling,
} if true

reasons_for(denials) := [sprintf("%v backend permitted for %v", [sovereignty, input.classification])] if {
	count(denials) == 0
	sovereignty := input.sovereignty
} else := [reason | some reason in denials]

# ---------------------------------------------------------------------- denials

denials contains sprintf(
	"%v content may never reach an external backend",
	[input.classification],
) if {
	input.sovereignty == "external"
	common.sovereign_required(input.classification)
}

denials contains sprintf(
	"an external backend is capped at C%v and this request carries %v",
	[common.max_external, input.classification],
) if {
	input.sovereignty == "external"
	common.level(input.classification) > common.max_external
}

denials contains "the backend's sovereignty is undeclared" if {
	not input.sovereignty in {"local", "external"}
}

denials contains "a request without a tenant cannot be scoped" if {
	not common.has_tenant
}

# ------------------------------------------------------------------ obligations

# Anything leaving the perimeter is recorded, whatever its classification.
obligations_for(request) := ["ledger_record", "redact_pii"] if {
	request.sovereignty == "external"
} else := []
