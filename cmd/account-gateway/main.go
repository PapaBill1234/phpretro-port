// SPDX-License-Identifier: GPL-3.0-only
package main

import (
	"context"
	"flag"
	"fmt"
	gateway "github.com/PapaBill1234/phpretro-port/internal/accountgateway"
	ui "github.com/PapaBill1234/phpretro-port/internal/accountui"
	vault "github.com/PapaBill1234/phpretro-port/internal/accountvault"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

func main() {
	port := flag.Int("port", 1456, "local loopback port")
	dir := flag.String("state-dir", "", "absolute private credential directory")
	flag.Parse()
	if *dir == "" || *port < 1 || *port > 65535 || flag.NArg() != 0 {
		log.Fatal("Invalid local account gateway configuration.")
	}
	state, e := vault.Open(*dir)
	if e != nil {
		log.Fatal("Private account storage unavailable or already locked.")
	}
	defer state.Close()
	addr := "127.0.0.1:" + strconv.Itoa(*port)
	listener, e := net.Listen("tcp4", addr)
	if e != nil {
		log.Fatal("Local callback port unavailable.")
	}
	defer listener.Close()
	app, e := ui.New(state, "http://"+addr+"/auth/callback", gateway.OpenAIEndpoints(), nil)
	if e != nil {
		log.Fatal("Trusted identity-provider configuration unavailable.")
	}
	server := &http.Server{Handler: app, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	results := make(chan error, 1)
	go func() { results <- server.Serve(listener) }()
	fmt.Println("Account manager ready at http://" + addr + "/")
	select {
	case e := <-results:
		if e != http.ErrServerClosed {
			log.Print("Local account server stopped.")
		}
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if server.Shutdown(shutdown) != nil {
			server.Close()
		}
	}
}
