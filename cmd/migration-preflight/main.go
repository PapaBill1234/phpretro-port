package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"github.com/PapaBill1234/phpretro-port/internal/migrationplan"
	"io"
	"os"
)

func main() {
	path := flag.String("input", "-", "JSON inventory file, or - for stdin")
	flag.Parse()
	var r io.Reader = os.Stdin
	var f *os.File
	if *path != "-" {
		var e error
		f, e = os.Open(*path)
		if e != nil {
			fmt.Fprintln(os.Stderr, "malformed_input")
			os.Exit(2)
		}
		defer f.Close()
		r = f
	}
	in, e := migrationplan.Parse(r)
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(2)
	}
	p := migrationplan.Build(in)
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(p)
	if p.Blocked() {
		os.Exit(3)
	}
}
