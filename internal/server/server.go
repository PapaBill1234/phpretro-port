package server

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/PapaBill1234/phpretro-port/internal/storage"
)

type Backend interface {
	Site(context.Context) (storage.Site, error)
	DatabaseReady(context.Context) error
	RedisReady(context.Context) error
}

func Handler(backend Backend) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/health", func(w http.ResponseWriter, r *http.Request) { respond(w, 200, map[string]string{"status": "ok"}) })
	mux.HandleFunc("GET /api/v1/ready", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 4*time.Second)
		defer cancel()
		database := backend.DatabaseReady(ctx) == nil
		cache := backend.RedisReady(ctx) == nil
		status, code := "ok", 200
		if !database || !cache {
			status, code = "degraded", 503
		}
		respond(w, code, map[string]any{"status": status, "database": database, "redis": cache})
	})
	mux.HandleFunc("GET /api/v1/site", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 4*time.Second)
		defer cancel()
		site, err := backend.Site(ctx)
		if err != nil {
			respond(w, 503, map[string]any{"error": map[string]string{"code": "site_unavailable", "message": "Site settings are unavailable."}})
			return
		}
		if backend.RedisReady(ctx) != nil {
			site.Health = "degraded"
		}
		respond(w, 200, site)
	})
	return mux
}

func respond(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
