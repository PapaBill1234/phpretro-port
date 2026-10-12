package migrationplan

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
	"unicode/utf8"
)

const Version = 1
const MaxBytes int64 = 4 << 20
const MaxRows = 10000
const MaxDepth = 64

var relationships = map[string]bool{"group_member": true, "message": true, "friend": true, "ownership": true}

type Input struct {
	Version       int            `json:"version"`
	LegacyUsers   []LegacyUser   `json:"legacy_users"`
	TargetUsers   []TargetUser   `json:"target_users"`
	Relationships []Relationship `json:"relationships"`
	Domains       []Domain       `json:"domains"`
	Schema        SchemaFacts    `json:"schema"`
}
type LegacyUser struct {
	ID               int64  `json:"id"`
	Username         string `json:"username"`
	ProposedTargetID *int64 `json:"proposed_target_id"`
}
type TargetUser struct {
	ID       int64  `json:"id"`
	Username string `json:"username"`
}
type Relationship struct {
	Kind     string `json:"kind"`
	SourceID int64  `json:"source_id"`
	TargetID int64  `json:"target_id"`
}
type Domain struct {
	Name      string `json:"name"`
	Supported bool   `json:"supported"`
}
type SchemaFacts struct {
	Target            string `json:"target"`
	Collation         string `json:"collation"`
	CollationVerified bool   `json:"collation_verified"`
}
type Plan struct {
	Version            int                    `json:"version"`
	ReportOnly         bool                   `json:"report_only"`
	Blockers           []string               `json:"blockers"`
	Users              []UserDecision         `json:"users"`
	Relationships      []RelationshipDecision `json:"relationships"`
	UnsupportedDomains []string               `json:"unsupported_domains"`
	Digest             string                 `json:"digest"`
}
type UserDecision struct {
	SourceID int64  `json:"source_id"`
	TargetID *int64 `json:"target_id"`
	Username string `json:"username"`
	Decision string `json:"decision"`
}
type RelationshipDecision struct {
	Kind     string `json:"kind"`
	SourceID int64  `json:"source_id"`
	TargetID int64  `json:"target_id"`
	Decision string `json:"decision"`
}

func Parse(r io.Reader) (Input, error) {
	b, e := io.ReadAll(io.LimitReader(r, MaxBytes+1))
	if e != nil {
		return Input{}, fmt.Errorf("input_read_error")
	}
	if int64(len(b)) > MaxBytes {
		return Input{}, fmt.Errorf("input_too_large")
	}
	if !utf8.Valid(b) {
		return Input{}, fmt.Errorf("invalid_utf8")
	}
	d := json.NewDecoder(bytes.NewReader(b))
	if e := walk(d, 0); e != nil {
		return Input{}, e
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return Input{}, fmt.Errorf("trailing_json")
	}
	var in Input
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if e := dec.Decode(&in); e != nil {
		return Input{}, fmt.Errorf("invalid_input")
	}
	if in.Version != Version {
		return Input{}, fmt.Errorf("unsupported_version")
	}
	if len(in.LegacyUsers)+len(in.TargetUsers)+len(in.Relationships)+len(in.Domains) > MaxRows {
		return Input{}, fmt.Errorf("row_limit")
	}
	return in, nil
}
func walk(d *json.Decoder, depth int) error {
	if depth > MaxDepth {
		return fmt.Errorf("nesting_limit")
	}
	t, e := d.Token()
	if e != nil {
		return fmt.Errorf("invalid_input")
	}
	delim, ok := t.(json.Delim)
	if !ok {
		return nil
	}
	switch delim {
	case '{':
		seen := map[string]bool{}
		for d.More() {
			k, e := d.Token()
			if e != nil {
				return fmt.Errorf("invalid_input")
			}
			key, ok := k.(string)
			if !ok {
				return fmt.Errorf("invalid_input")
			}
			if seen[key] {
				return fmt.Errorf("duplicate_json_key")
			}
			seen[key] = true
			if prohibited(key) {
				return fmt.Errorf("prohibited_field")
			}
			if e = walk(d, depth+1); e != nil {
				return e
			}
		}
		if _, e = d.Token(); e != nil {
			return fmt.Errorf("invalid_input")
		}
	case '[':
		for d.More() {
			if e = walk(d, depth+1); e != nil {
				return e
			}
		}
		if _, e = d.Token(); e != nil {
			return fmt.Errorf("invalid_input")
		}
	}
	return nil
}
func prohibited(k string) bool {
	k = strings.ToLower(k)
	for _, s := range []string{"password", "hash", "credential", "secret", "token"} {
		if strings.Contains(k, s) {
			return true
		}
	}
	return false
}
func Build(in Input) Plan {
	p := Plan{Version: Version, ReportOnly: true, Blockers: []string{"unverified_schema_collation"}}
	src := map[int64]bool{}
	proposed := map[int64]bool{}
	targets := map[int64]bool{}
	names := map[string]bool{}
	for _, t := range in.TargetUsers {
		if t.ID <= 0 {
			p.Blockers = append(p.Blockers, "invalid_target_id")
		}
		if targets[t.ID] {
			p.Blockers = append(p.Blockers, "duplicate_target_id")
		}
		targets[t.ID] = true
		if t.Username == "" || len([]rune(t.Username)) > 25 {
			p.Blockers = append(p.Blockers, "invalid_target_username")
		}
		if names[strings.ToLower(t.Username)] {
			p.Blockers = append(p.Blockers, "possible_username_collision")
		}
		names[strings.ToLower(t.Username)] = true
	}
	for _, u := range in.LegacyUsers {
		if u.ID <= 0 {
			p.Blockers = append(p.Blockers, "invalid_source_id")
		}
		if src[u.ID] {
			p.Blockers = append(p.Blockers, "duplicate_source_id")
		}
		src[u.ID] = true
		if u.Username == "" || len([]rune(u.Username)) > 25 {
			p.Blockers = append(p.Blockers, "invalid_source_username")
		}
		key := strings.ToLower(u.Username)
		if names[key] {
			p.Blockers = append(p.Blockers, "possible_username_collision")
		}
		names[key] = true
		var tid *int64
		decision := "unresolved"
		if u.ProposedTargetID != nil && *u.ProposedTargetID > 0 {
			v := *u.ProposedTargetID
			tid = &v
			decision = "proposed_explicit_mapping"
			if proposed[v] {
				p.Blockers = append(p.Blockers, "duplicate_proposed_target_id")
			}
			proposed[v] = true
			if targets[v] {
				p.Blockers = append(p.Blockers, "target_id_overlap")
			}
		} else {
			p.Blockers = append(p.Blockers, "missing_target_mapping")
		}
		p.Users = append(p.Users, UserDecision{u.ID, tid, u.Username, decision})
	}
	for i := range p.Users {
		if p.Users[i].TargetID != nil && !targets[*p.Users[i].TargetID] {
			p.Blockers = append(p.Blockers, "target_mapping_not_verified")
		}
	}
	for _, r := range in.Relationships {
		d := "preserved"
		if r.SourceID <= 0 || r.TargetID <= 0 || !src[r.SourceID] || (!targets[r.TargetID] && !proposed[r.TargetID]) {
			d = "unresolved_orphan"
			p.Blockers = append(p.Blockers, "orphan_relationship")
		}
		if !relationships[r.Kind] {
			d = "unsupported_relationship_kind"
			p.Blockers = append(p.Blockers, "unsupported_relationship_kind")
		}
		p.Relationships = append(p.Relationships, RelationshipDecision{r.Kind, r.SourceID, r.TargetID, d})
	}
	for range in.Domains {
		p.UnsupportedDomains = append(p.UnsupportedDomains, "unsupported_domain")
		p.Blockers = append(p.Blockers, "unsupported_domain")
	}
	sort.Strings(p.Blockers)
	p.Blockers = unique(p.Blockers)
	sort.Slice(p.Users, func(i, j int) bool {
		a, b := p.Users[i], p.Users[j]
		if a.SourceID != b.SourceID {
			return a.SourceID < b.SourceID
		}
		at, bt := int64(0), int64(0)
		if a.TargetID != nil {
			at = *a.TargetID
		}
		if b.TargetID != nil {
			bt = *b.TargetID
		}
		if at != bt {
			return at < bt
		}
		if a.Username != b.Username {
			return a.Username < b.Username
		}
		return a.Decision < b.Decision
	})
	sort.Slice(p.Relationships, func(i, j int) bool {
		a, b := p.Relationships[i], p.Relationships[j]
		if a.Kind != b.Kind {
			return a.Kind < b.Kind
		}
		if a.SourceID != b.SourceID {
			return a.SourceID < b.SourceID
		}
		if a.TargetID != b.TargetID {
			return a.TargetID < b.TargetID
		}
		return a.Decision < b.Decision
	})
	sort.Strings(p.UnsupportedDomains)
	raw, _ := json.Marshal(p)
	h := sha256.Sum256(raw)
	p.Digest = hex.EncodeToString(h[:])
	return p
}
func unique(a []string) []string {
	r := a[:0]
	for _, s := range a {
		if len(r) == 0 || r[len(r)-1] != s {
			r = append(r, s)
		}
	}
	return r
}
func (p Plan) Blocked() bool { return len(p.Blockers) > 0 }
