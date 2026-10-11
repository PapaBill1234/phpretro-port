package main

import (
	"context"
	"flag"
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
	flag.Parse()
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
	srv := &http.Server{Addr: ":8080", Handler: server.Handler(backend), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 1 << 20}
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
