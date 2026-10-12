package main

import (
	"context"
	"flag"
	"github.com/PapaBill1234/phpretro-port/internal/auth"
	"github.com/PapaBill1234/phpretro-port/internal/polaris"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/PapaBill1234/phpretro-port/internal/server"
	"github.com/PapaBill1234/phpretro-port/internal/storage"
)

func main() {
	migrate := flag.Bool("migrate", false, "apply CMS migrations with CMS_MIGRATION_DSN and exit")
	grant := flag.Int64("grant-admin", 0, "operator-only: enroll an existing Polaris user for CMS staff MFA")
	revokeID := flag.Int64("revoke-admin", 0, "operator-only: revoke CMS staff access")
	enrollmentFile := flag.String("enrollment-file", "", "new private mode-0600 enrollment file")
	flag.Parse()
	commands := 0
	if *migrate {
		commands++
	}
	if *grant != 0 {
		commands++
	}
	if *revokeID != 0 {
		commands++
	}
	if commands > 1 || flag.NArg() != 0 || (*enrollmentFile != "" && *grant == 0) {
		log.Fatal("invalid command arguments")
	}
	if *grant != 0 || *revokeID != 0 {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		var err error
		if *grant != 0 {
			err = enrollment(ctx, *grant, *enrollmentFile)
		} else {
			err = revoke(ctx, *revokeID)
		}
		if err != nil {
			log.Fatal("staff operation failed; check identity, migration grants and private enrollment destination")
		}
		log.Print("staff operation complete")
		return
	}
	if *migrate {
		db, err := storage.OpenDB(os.Getenv("CMS_MIGRATION_DSN"))
		if err != nil {
			log.Fatal("migration database configuration unavailable")
		}
		defer db.Close()
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if storage.Migrate(ctx, db) != nil {
			log.Fatal("CMS migration failed; inspect database configuration and migration state")
		}
		log.Print("CMS migration complete")
		return
	}
	backend, err := storage.Open(os.Getenv("CMS_DATABASE_DSN"), os.Getenv("REDIS_ADDR"), os.Getenv("REDIS_PASSWORD"))
	if err != nil {
		log.Fatal("CMS configuration unavailable")
	}
	defer backend.Close()
	game, err := storage.OpenDB(os.Getenv("POLARIS_DATABASE_DSN"))
	if err != nil {
		log.Fatal("game identity configuration unavailable")
	}
	defer game.Close()
	identity := &polaris.Accounts{DB: game}
	key, err := staffKey()
	if err != nil {
		log.Fatal("staff encryption configuration unavailable")
	}
	accounts, err := auth.New(identity, backend, backend.Cache, key)
	if err != nil {
		log.Fatal("authentication configuration unavailable")
	}
	secure, err := secureCookies()
	if err != nil {
		log.Fatal("invalid cookie configuration")
	}
	config, err := server.NewAccountConfig(os.Getenv("CMS_PUBLIC_ORIGIN"), secure, os.Getenv("CMS_TRUSTED_PROXY_CIDRS"))
	if err != nil {
		log.Fatal("invalid origin or proxy configuration")
	}
	srv := &http.Server{Addr: ":8080", Handler: server.HandlerWithAccounts(backend, accounts, config, identity.Ready), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 1 << 20}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	result := make(chan error, 1)
	go func() { result <- srv.ListenAndServe() }()
	select {
	case err := <-result:
		if err != http.ErrServerClosed {
			log.Fatal("CMS HTTP server failed")
		}
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if srv.Shutdown(shutdown) != nil {
			_ = srv.Close()
		}
	}
}
