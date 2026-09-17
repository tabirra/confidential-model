# Permissive KBS resource policy for Layer 3 development/demo use.
#
# Trustee's KBS evaluates this OPA policy against the Attestation Service's
# verdict for the requesting guest (its parsed TEE evidence) before
# releasing ANY resource, including the model decryption key stored at
# e.g. default/key/my-model. This policy only requires that attestation
# produced *some* verified TEE evidence ("sample" attester in
# kata-qemu-coco-dev dev mode counts) — it does not check specific
# measurements, SVNs, or claims.
#
# This is intentionally permissive, matching Layer 3's scope ("a
# permissive resource policy ... allowing key release to sample TEE
# attestation"). For anything beyond a demo, replace `allow` with checks
# against the real attestation claims Trustee's Attestation Service (AS)
# hands to this policy — e.g. pin `input.submods.cpu["ear.trustworthiness-vector"]`
# fields, the TEE type, or a measurement allowlist. See the Trustee docs:
# https://github.com/confidential-containers/trustee
package policy

default allow = false

# Release the resource once AS has attached a trustworthiness verdict to
# the request, i.e. the guest completed remote attestation at all.
allow {
	input.submods
}
