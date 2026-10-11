// SPDX-License-Identifier: GPL-3.0-only
import type { LocaleId } from "./site.js";

const en = {
  explore: "Explore", project: "The project", preview: "Preview", theme: "Theme", language: "Language",
  modern: "Modern", legacy: "Classic", eyebrow: "A little world. A lot of possibility.",
  title: "Make room for your next story.", intro: "A community hotel, rebuilt for the web. Find your people, make your space, and let your imagination move in.",
  cta: "Explore the project", secondary: "Discover the classic look", artLabel: "Original geometric illustration of a colorful neighborhood",
  foundation: "A fresh start, with familiar possibilities.", foundationText: "The website foundation is here. Accounts, rooms, and hotel access are the next chapters.",
  card1: "Find your people", text1: "A place for conversations, shared interests, and new friendships.",
  card2: "Make it your own", text2: "Personal spaces and expressive styles, ready for your imagination.",
  card3: "Build something together", text3: "A community shaped by its people, one good idea at a time.",
  loading: "Connecting to the site service…", offline: "The site service is unavailable. Please try again in a moment.",
  retry: "Try again", degraded: "The site is available, with reduced service capacity.",
  connected: "Site service connected", previewNote: "Preview controls change this browser view only.",
  footer: "Development preview · Accounts and hotel access are being built.", original: "Original interface and artwork."
};
export type Messages = { [Key in keyof typeof en]: string };
const nl: Messages = {
  explore: "Ontdek", project: "Het project", preview: "Voorbeeld", theme: "Thema", language: "Taal",
  modern: "Modern", legacy: "Klassiek", eyebrow: "Een kleine wereld. Vol mogelijkheden.",
  title: "Maak ruimte voor je volgende verhaal.", intro: "Een communityhotel, opnieuw gebouwd voor het web. Vind jouw mensen, maak jouw plek en laat je fantasie thuiskomen.",
  cta: "Ontdek het project", secondary: "Ontdek de klassieke stijl", artLabel: "Originele geometrische illustratie van een kleurrijke buurt",
  foundation: "Een frisse start, met vertrouwde mogelijkheden.", foundationText: "De basis van de website staat. Accounts, kamers en toegang tot het hotel zijn de volgende hoofdstukken.",
  card1: "Vind jouw mensen", text1: "Een plek voor gesprekken, gedeelde interesses en nieuwe vriendschappen.",
  card2: "Maak het van jou", text2: "Persoonlijke ruimtes en eigen stijlen, klaar voor jouw fantasie.",
  card3: "Bouw samen iets moois", text3: "Een community gevormd door haar mensen, één goed idee tegelijk.",
  loading: "Verbinding maken met de website…", offline: "De websiteservice is niet beschikbaar. Probeer het straks opnieuw.",
  retry: "Opnieuw proberen", degraded: "De website is beschikbaar met beperkte servicecapaciteit.",
  connected: "Websiteservice verbonden", previewNote: "Deze voorbeeldinstellingen wijzigen alleen dit browservenster.",
  footer: "Ontwikkelvoorbeeld · Accounts en hoteltoegang worden gebouwd.", original: "Originele interface en illustraties."
};
export const messages: Record<LocaleId, Messages> = { en, nl };
