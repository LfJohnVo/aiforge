# Who may see a document.
#
# This is the policy behind identity-aware retrieval. It answers about **one** candidate
# document; the filter that keeps a user from learning a document exists is pushed into
# the vector query itself (see knowledge/access_control.py). Both must agree, and this is
# the readable statement of the rule they both implement.
package peak.knowledge

import data.peak.common

default decision := {
	"allow": false,
	"reasons": ["default deny"],
	"obligations": [],
}

decision := {
	"allow": allow,
	"reasons": reasons,
	"obligations": obligations,
	"ceiling": input.ceiling,
} if {
	allow := count(denials) == 0
	reasons := reasons_for(denials)
	obligations := obligations_for(input)
}

reasons_for(denials) := ["knowledge access permitted"] if {
	count(denials) == 0
} else := [reason | some reason in denials]

# ---------------------------------------------------------------------- denials

denials contains "anonymous requesters may only read C0 material" if {
	not input.authenticated
	common.level(input.classification) > 0
}

denials contains sprintf(
	"content is %v but the requester's ceiling is %v",
	[input.classification, input.ceiling],
) if {
	common.level(input.classification) > common.level(input.ceiling)
}

denials contains "the document's ACL names groups the requester does not hold" if {
	count(input.acl_groups) > 0
	not common.in_any_group(input.groups, input.acl_groups)
}

denials contains "a request without a tenant cannot be scoped" if {
	not common.has_tenant
}

# ------------------------------------------------------------------ obligations

# Restricted material stays in the answer only with its citation attached: an unsourced
# C3 claim is the one an auditor cannot trace back.
obligations_for(request) := ["cite_source"] if {
	common.level(request.classification) >= 3
} else := []
