# Which tools reach the model, and which may actually be invoked.
#
# Evaluated twice on purpose: once when the catalogue is built, so a forbidden tool is
# never described to the model, and once immediately before invocation, because the
# catalogue was built before the task accumulated its classification.
package peak.tools

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
	"autonomy": required_autonomy,
} if {
	required_autonomy := input.autonomy_declared
}

reasons_for(denials) := ["tool use permitted"] if {
	count(denials) == 0
} else := [reason | some reason in denials]

# ---------------------------------------------------------------------- denials

denials contains "an anonymous requester may not invoke tools" if {
	not input.authenticated
}

denials contains "a tool call needs a named tool" if {
	not named_tool
}

named_tool if {
	input.resource != ""
}

denials contains "a request without a tenant cannot be scoped" if {
	not common.has_tenant
}

denials contains sprintf(
	"the task carries %v, above the requester's ceiling %v",
	[input.classification, input.ceiling],
) if {
	common.level(input.classification) > common.level(input.ceiling)
}

# A tool that would act on restricted data needs more than the default grant: an
# automated A0/A1 path must not be the one that touches C3/C4.
denials contains "acting on restricted data requires an approved action" if {
	common.sovereign_required(input.classification)
	not common.needs_human(input.autonomy_declared)
}

# ------------------------------------------------------------------ obligations

obligations contains "human_approval" if {
	common.needs_human(input.autonomy_declared)
}

obligations contains "two_approvers" if {
	common.autonomy(input.autonomy_declared) >= 4
}

obligations contains "elevated_role" if {
	common.autonomy(input.autonomy_declared) >= 3
}

obligations contains "ledger_record" if {
	common.needs_human(input.autonomy_declared)
}

obligations_for(_) := [obligation | some obligation in obligations]
