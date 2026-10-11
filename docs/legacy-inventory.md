# PHPRetro baseline and migration inventory

Status: source inventory for planning; no runtime parity or migration has been implemented.

The baseline is [Quackster/PHPRetro at `8ab8f81e6bcd09fc661547cb032ef045149c095e`](https://github.com/Quackster/PHPRetro/tree/8ab8f81e6bcd09fc661547cb032ef045149c095e), inspected on 2026-10-11. Its [version file](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/version.php#L19) identifies PHPRetro 4.0.10 BETA, revision 85, dated October 2009. This repository is a historical implementation, not a description of current habbo.com.

The snapshot has **238 PHP files**, including **34 root page scripts**, **132 habblet scripts**, **22 housekeeping scripts**, and **8 SWF files**. File existence means a feature is present in source; it does not prove it works against Polaris or even runs unchanged today. The exact page/endpoint inventory below is the starting coverage checklist. Each feature must acquire a behavior specification, target contract, test, and capability status before being marked complete.

## Product modules

| Module | Legacy behavior to characterize | Target ownership and migration gap |
| --- | --- | --- |
| Identity and account | Registration, username checks, login/logout, remember-me, reauthentication, email verification, password recovery/settings | Go identity service and Polaris adapter; preserve account-ID mapping, determine imported password compatibility, replace session/recovery/SSO security behavior |
| Website content | Landing, personal dashboard, news/articles/archive/RSS, community, campaign banners, recommended groups, static papers/history | Go content APIs with React theme view models; import website records and validate HTML instead of carrying PHP templates forward |
| Avatar and wardrobe | Figure selection, gender/club restrictions, saved appearances, avatar previews | Figure contract and renderer capability from the actual target client/resource pack; standalone React editor replacement for SWF |
| Social | Friends/categories, tags, group membership/roles/favorites, minimail | Some records belong to the emulator, some to the CMS; explicit table/field map and authorization tests required |
| Homes and groups | Drag/drop stickers, backgrounds, notes/widgets, edit sessions, inventory/store, group page settings, ratings, guestbooks, badge editor | CMS records plus emulator group contract; import coordinates/z-order and ownership; theme slots must support or explicitly report absent modules |
| Discussions | Group topics/posts, previews, delete/settings operations | CMS data import and per-group authorization; moderation, output sanitization and concurrency must be specified |
| Economy | Club subscriptions/gifts, vouchers, collectables, credit/furniture awards, home shop purchases | Polaris adapter owns game writes; website transaction history belongs to CMS; prevent duplicate fulfillment and partial cross-schema updates |
| Hotel launch/status | Hotel presence/count, client launch, errors/reconnect, alerts, realtime admin updates | Octane/Polaris compatibility milestone distinct from CMS parity; replace Holograph MUS commands with verified target capabilities |
| Support and moderation | FAQ/search, support tickets, reports, bans, logs, housekeeping user editing | Go administration APIs and audit records; translate ranks into explicit capabilities; do not expose browser direct database access |
| System configuration | Maintenance, caching, email/newsletters, settings, installation/upgrades | Validated MariaDB migrations, Redis caching, runtime locale/theme configuration; safe setup/bootstrap instead of legacy installer |

## Data and integration contracts

**MariaDB is the selected SQL database for both CMS and emulator.** Keep separate schemas and database roles where practical even on one MariaDB server. The CMS owns its schema migrations; it does not silently rewrite Polaris-owned tables. Redis is for expiring session/cache/rate-limit state, not the only durable copy of business records. A schema-access matrix and adapter contract should specify allowed reads/writes per capability.

PHPRetro has an existing emulator boundary: [`includes/core.php:43`](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/core.php#L43) dynamically loads a server definition. The only bundled definition is [Holograph](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/data/holograph.php). Its interface is a collection of page-specific SQL classes, not a portable domain API. Replace that boundary with typed Go interfaces and a versioned Polaris implementation.

| Contract | Observed source | Required target decision |
| --- | --- | --- |
| Account names | Game account `users.name`; prefixed CMS metadata also has `name` | Map target `username` explicitly; inspect uniqueness, case folding and allowed characters rather than renaming blindly |
| Account IDs | Login joins game `users.id` with prefixed website `users.id` | Stable imported-ID mapping; registration must handle dual-record creation/reconciliation |
| Passwords | `sha1(password + lowercase username)` | Read actual imported format; limited legacy verification and rehash or reset; never assume Polaris expects legacy SHA-1 |
| SSO | `users.ticket_sso`, generated with `mt_rand`, refreshed by user update | Match Polaris/Octane launch and validation contract; cryptographic generation, lifetime, invalidation and replay behavior require tests |
| Game state | Rooms, groups, memberships, friends, badges, club, bans, currencies, vouchers, songs, presence and chat log are queried from game tables | Field-by-field capability matrix against pinned Polaris; unsupported capabilities stay tracked rather than emulated with unchecked SQL |
| Realtime actions | Raw Holograph MUS commands for user/credits/club/ban/alert updates | Target protocol/API or deliberately added emulator endpoint; a database update alone may leave online state stale |
| Website state | Prefixed content/home/forum/minimail/settings/support tables | Import with a dry-run report, validation and rollback plan; normalization can change physical schema while preserving intended behavior |

Evidence: [identity reads/login join](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/data/holograph.php#L33), [SSO update](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/data/holograph.php#L99), [password hash](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/classes.php#L38), [ticket generator](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/functions.php#L40), [MUS sender](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/functions.php#L62), [admin notification calls](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/housekeeping/users.php#L58).

The installer defines prefixed website tables: `alerts`, `banners`, `campaigns`, `client_errors`, `collectables`, `faq`, `forum_posts`, `forum_threads`, `guestbook`, `help`, `homes`, `homes_catalogue`, `homes_edit`, `minimail`, `news`, `ratings`, `recommended`, `security_log`, `settings`, `settings_pages`, `tags`, `transactions`, `users`, `verify`, and `wardrobe`. Most use MyISAM and legacy UTF-8 definitions. This is an inventory, not a schema to reuse unchanged. [Installer table definitions](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/install/install_functions.php#L104).

## Themes and localization

The original uses fixed PHP templates with hardcoded gallery paths, inline HTML/scripts and widget hooks. There is no dynamic theme-package system to port. Build the proposed React theme interface around stable DTOs, tokens, layouts, page/widget slots, locale keys and declared optional capabilities. Require theme contract tests and compatibility versions. Adding a theme must not change identity, permissions, schema or SSO code. An imported open-source theme needs its own license/provenance and a frontend adapter; arbitrary server-side PHP templates cannot be dropped into React.

Only [`includes/languages/en.php`](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/languages/en.php) is bundled. The locale loader selects switch groups from that executable PHP file. `site_language` is a hidden setting, and the installer describes language choice as permanent. Runtime housekeeping switching, per-user locale and translation of newly created content are new features. Use data catalogues, locale validation/fallback and separately translated content fields; do not translate user-created text automatically. [Loader](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/classes.php#L535), [hidden setting](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/install/install_functions.php#L415), [installer statement](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/install/install.php#L117).

## Source gaps that affect parity

- Events are disabled by default: the adapter returns rooms with ID zero; the real event query is commented out. Characterize and choose whether to implement events as an additional capability. [Evidence](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/data/holograph.php#L240).
- The snapshot has no automated test suite or root dependency manifest. Baseline behavior must be recorded before claiming parity. Existing APIs use PHP `mysql_*`, Apache rewrites, GD imaging, sockets and `mail()`, with Prototype 1.6.0.2 and SWFObject 1.5 in vendored scripts.
- Installation is destructive: it drops website tables and includes an adapter method that truncates game users. New migration tooling must be separately designed; do not execute upstream installers on a real dataset. [Website drops](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/install/install_functions.php#L80), [user truncation](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/data/holograph.php#L19).
- Avatar previews fetch official Habbo imaging and cache results. The original-assets requirement extends to avatars/badges/sample packs, not only theme backgrounds. [Imaging behavior](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/classes.php#L235).
- This source's hotel page embeds an external Shockwave **DCR**, not one of the 8 bundled CMS SWFs. Octane launch compatibility is a separate deliverable from widget modernization. [Client embed](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/client.php#L93).

## Code license and asset provenance

The upstream repository description and PHP headers identify PHPRetro code as GPL 3.0. Its headers separately attribute images, scripts and layouts to Sulake. No standalone root license file was found in the inspected snapshot. Keep an explicit source-origin record, preserve applicable PHPRetro code notices, and choose a compatible license for the port based on the code being adapted. Record component-specific notices: for example, the bundled SWFObject identifies its own MIT license. GPL code notices do not establish permission for all bundled media or every vendored script. [Upstream notices](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/core.php#L3), [SWFObject notice](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/web-gallery/static/js/libs2.js#L2).

The delivery repository should contain newly written React markup/CSS and newly produced artwork/audio, with an asset manifest containing path, author, creation/source method, license, source URL when applicable, revision/hash and review status. Use the legacy layout as an observed reference and record design measurements rather than copying gallery images, fonts, CSS bundles, SWF resources or audio. The companion [Flash modernization plan](flash-modernization.md) details the production pipeline and game-resource boundary.

## Exact file inventory

Names below are source artifacts, not the target route structure. URL compatibility is defined by the [legacy rewrite rules](https://github.com/Quackster/PHPRetro/blob/8ab8f81e6bcd09fc661547cb032ef045149c095e/.htaccess), not solely PHP filenames. Existing themes may omit some modules; the target should explicitly declare that capability rather than fail at runtime.

### Root pages — 34 files

Source: [.](https://github.com/Quackster/PHPRetro/tree/8ab8f81e6bcd09fc661547cb032ef045149c095e).

```text
account.php
articles.php
client.php
clientutils.php
club.php
collectables.php
community.php
credits.php
discussions.php
email.php
error.php
forgot.php
groups.php
help.php
history.php
home.php
index.php
intermediate.php
iot.php
landing.php
login_popup.php
logout.php
maintenance.php
maintenance_new.php
me.php
papers.php
pixels.php
profile.php
reauthenticate.php
register.php
security_check.php
tag.php
tryout.php
welcome.php
```

### Housekeeping — 22 files

Source: [housekeeping](https://github.com/Quackster/PHPRetro/tree/8ab8f81e6bcd09fc661547cb032ef045149c095e/housekeeping).

```text
about.php
alerts.php
banners.php
bans.php
cache.php
campaigns.php
catalogue.php
collectables.php
dashboard.php
faq.php
favicon.ico
help.php
index.php
logout.php
logs.php
news.php
newsletter.php
permissions.php
recommended.php
settings.php
updates.php
users.php
vouchers.php
```

### Shared templates

Source: [templates](https://github.com/Quackster/PHPRetro/tree/8ab8f81e6bcd09fc661547cb032ef045149c095e/templates).

```text
.htaccess
client_footer.php
client_header.php
community_footer.php
community_header.php
email_footer.php
email_header.php
faq_footer.php
faq_header.php
housekeeping_footer.php
housekeeping_header.php
iot_footer.php
iot_header.php
login_footer.php
login_header.php
maintenance_footer.php
maintenance_header.php
maintenance_new_footer.php
maintenance_new_header.php
myhabbo_footer.php
newsletter_body.php
newsletter_footer.php
newsletter_header.php
register_header.php
```

### Core runtime

Source: [includes](https://github.com/Quackster/PHPRetro/tree/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes).

```text
.htaccess
classes.php
core.php
functions.php
hksession.php
session.php
version.php
```

### Emulator adapters

Source: [includes/data](https://github.com/Quackster/PHPRetro/tree/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/data).

```text
holograph.php
```

### Language catalogues

Source: [includes/languages](https://github.com/Quackster/PHPRetro/tree/8ab8f81e6bcd09fc661547cb032ef045149c095e/includes/languages).

```text
en.php
```

### XML/RSS/promo endpoints and data

Source: [xml](https://github.com/Quackster/PHPRetro/tree/8ab8f81e6bcd09fc661547cb032ef045149c095e/xml).

```text
badge_data.xml
badge_editor.xml
draworder.xml
figure_editor.xml
figuredata.xml
landing_intro.xml
promo_habbos.php
promo_habbos_v2.php
rss.php
```

### Habblet endpoints — 132 files

Source: [habblet/](https://github.com/Quackster/PHPRetro/tree/8ab8f81e6bcd09fc661547cb032ef045149c095e/habblet). Every PHP endpoint is included below; group names are inventory buckets, not proposed API namespaces.

#### ajax (17)

```text
ajax_addFriend.php
ajax_collectiblesConfirm.php
ajax_collectiblesPurchase.php
ajax_confirmAddFriend.php
ajax_emailcheck.php
ajax_habboclub_enddate.php
ajax_habboclub_gift.php
ajax_load_events.php
ajax_mgmgetinvitelink.php
ajax_namecheck.php
ajax_password.php
ajax_redeemvoucher.php
ajax_removeFeedItem.php
ajax_tagfight.php
ajax_tagmatch.php
ajax_tagsearch.php
ajax_updatemotto.php
```

#### components (1)

```text
components_updateHabboCount.php
```

#### discussions_actions (12)

```text
discussions_actions_confirm_delete_topic.php
discussions_actions_deletepost.php
discussions_actions_deletetopic.php
discussions_actions_newtopic.php
discussions_actions_opentopicsettings.php
discussions_actions_pingsession.php
discussions_actions_previewpost.php
discussions_actions_previewtopic.php
discussions_actions_savepost.php
discussions_actions_savetopic.php
discussions_actions_savetopicsettings.php
discussions_actions_updatepost.php
```

#### friendmanagement (7)

```text
friendmanagement_createcategory.php
friendmanagement_deletecategory.php
friendmanagement_deletefriends.php
friendmanagement_editCategory.php
friendmanagement_movefriends.php
friendmanagement_updatecategoryoptions.php
friendmanagement_viewcategory.php
```

#### grouppurchase (3)

```text
grouppurchase_group_create_form.php
grouppurchase_purchase_ajax.php
grouppurchase_purchase_confirmation.php
```

#### groups (1)

```text
groups_widgets.php
```

#### groups_actions (17)

```text
groups_actions_cancelEditingSession.php
groups_actions_check_group_url.php
groups_actions_confirm_delete_group.php
groups_actions_confirm_deselect_favorite.php
groups_actions_confirm_leave.php
groups_actions_confirm_select_favorite.php
groups_actions_delete_group.php
groups_actions_deselect_favorite.php
groups_actions_group_settings.php
groups_actions_join.php
groups_actions_leave.php
groups_actions_saveEditingSession.php
groups_actions_select_favorite.php
groups_actions_show_badge_editor.php
groups_actions_startEditingSession.php
groups_actions_update_group_badge.php
groups_actions_update_group_settings.php
```

#### habboclub (3)

```text
habboclub_habboclub_confirm.php
habboclub_habboclub_reminder_remove.php
habboclub_habboclub_subscribe.php
```

#### minimail (10)

```text
minimail_confirmReport.php
minimail_deleteMessage.php
minimail_emptyTrash.php
minimail_loadMessage.php
minimail_loadMessages.php
minimail_preview.php
minimail_recipients.php
minimail_report.php
minimail_sendMessage.php
minimail_undeleteMessage.php
```

#### mod (2)

```text
mod_add_report.php
mod_localizations.php
```

#### myhabbo_avatarlist (3)

```text
myhabbo_avatarlist_avatarinfo.php
myhabbo_avatarlist_friendsearchpaging.php
myhabbo_avatarlist_membersearchpaging.php
```

#### myhabbo_friends (1)

```text
myhabbo_friends_add.php
```

#### myhabbo_groups (12)

```text
myhabbo_groups_batch_accept.php
myhabbo_groups_batch_confirm_accept.php
myhabbo_groups_batch_confirm_decline.php
myhabbo_groups_batch_confirm_give_rights.php
myhabbo_groups_batch_confirm_remove.php
myhabbo_groups_batch_confirm_revoke_rights.php
myhabbo_groups_batch_decline.php
myhabbo_groups_batch_give_rights.php
myhabbo_groups_batch_remove.php
myhabbo_groups_batch_revoke_rights.php
myhabbo_groups_groupinfo.php
myhabbo_groups_memberlist.php
```

#### myhabbo_guestbook (5)

```text
myhabbo_guestbook_add.php
myhabbo_guestbook_configure.php
myhabbo_guestbook_list.php
myhabbo_guestbook_preview.php
myhabbo_guestbook_remove.php
```

#### myhabbo_homes.php (1)

```text
myhabbo_homes.php
```

#### myhabbo_linktool (1)

```text
myhabbo_linktool_search.php
```

#### myhabbo_noteeditor (3)

```text
myhabbo_noteeditor_editor.php
myhabbo_noteeditor_place.php
myhabbo_noteeditor_preview.php
```

#### myhabbo_rating (2)

```text
myhabbo_rating_rate.php
myhabbo_rating_reset_ratings.php
```

#### myhabbo_sticker (2)

```text
myhabbo_sticker_place_sticker.php
myhabbo_sticker_remove_sticker.php
```

#### myhabbo_stickie (2)

```text
myhabbo_stickie_delete.php
myhabbo_stickie_edit.php
```

#### myhabbo_store (9)

```text
myhabbo_store_background_warning.php
myhabbo_store_inventory.php
myhabbo_store_inventory_items.php
myhabbo_store_inventory_preview.php
myhabbo_store_items.php
myhabbo_store_main.php
myhabbo_store_preview.php
myhabbo_store_purchase.php
myhabbo_store_purchase_confirm.php
```

#### myhabbo_tag (6)

```text
myhabbo_tag_add.php
myhabbo_tag_addgrouptag.php
myhabbo_tag_list.php
myhabbo_tag_listgrouptags.php
myhabbo_tag_remove.php
myhabbo_tag_removegrouptag.php
```

#### myhabbo_traxplayer (1)

```text
myhabbo_traxplayer_select_song.php
```

#### myhabbo_widget (3)

```text
myhabbo_widget_add.php
myhabbo_widget_delete.php
myhabbo_widget_edit.php
```

#### myhabbo_widgets.php (1)

```text
myhabbo_widgets.php
```

#### other (6)

```text
habbosearchcontent.php
mytagslist.php
null.php
proxy.php
quickmenu.php
wardrobeStore.php
```

#### trax (1)

```text
trax_song.php
```

