package migrationplan

import (
	"strings"
	"testing"
)

func TestParseRejectsStrictInput(t *testing.T) {
	for _, tc := range []struct{ name, in, want string }{{"duplicate", "{\"version\":1,\"version\":1}", "duplicate_json_key"}, {"unknown", "{\"version\":1,\"nope\":1}", "invalid_input"}, {"secret", "{\"version\":1,\"password\":\"x\"}", "prohibited_field"}, {"trailing", "{\"version\":1} {}", "trailing_json"}, {"nested duplicate", "{\"version\":1,\"x\":{\"a\":1,\"a\":2}}", "duplicate_json_key"}} {
		t.Run(tc.name, func(t *testing.T) {
			_, e := Parse(strings.NewReader(tc.in))
			if e == nil || !strings.Contains(e.Error(), tc.want) {
				t.Fatalf("error=%v want %s", e, tc.want)
			}
		})
	}
}
func TestBuildConservativeAndPreserves(t *testing.T) {
	id := int64(42)
	in := Input{Version: 1, Schema: SchemaFacts{Target: "mariadb", CollationVerified: true}, LegacyUsers: []LegacyUser{{ID: 1, Username: "Alice", ProposedTargetID: &id}}, TargetUsers: []TargetUser{{ID: 42, Username: "Alice"}}, Relationships: []Relationship{{Kind: "friend", SourceID: 1, TargetID: 42}}}
	p := Build(in)
	if !p.ReportOnly || !p.Blocked() {
		t.Fatal("planner must remain report-only and blocked")
	}
	if len(p.Users) != 1 || p.Users[0].TargetID == nil || *p.Users[0].TargetID != 42 {
		t.Fatalf("mapping not preserved: %+v", p.Users)
	}
	if len(p.Relationships) != 1 {
		t.Fatal("relationship dropped")
	}
	if !contains(p.Blockers, "unverified_schema_collation") {
		t.Fatal("schema blocker missing")
	}
}
func TestBuildDeterministicBlocked(t *testing.T) {
	a := Input{Version: 1, LegacyUsers: []LegacyUser{{ID: 2, Username: "Bob"}, {ID: 1, Username: "Alice"}}}
	b := Input{Version: 1, LegacyUsers: []LegacyUser{{ID: 1, Username: "Alice"}, {ID: 2, Username: "Bob"}}}
	pa, pb := Build(a), Build(b)
	if pa.Digest != pb.Digest {
		t.Fatalf("digest differs %s %s", pa.Digest, pb.Digest)
	}
}
func contains(a []string, s string) bool {
	for _, x := range a {
		if x == s {
			return true
		}
	}
	return false
}
