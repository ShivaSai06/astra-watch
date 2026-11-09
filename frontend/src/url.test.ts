import { describe, expect, it } from "vitest";
import { cleanedUrl, stripTrackingParams } from "./url";

const loc = (search: string, hash = "#/") => ({ pathname: "/", search, hash });

describe("tracking parameter cleanup", () => {
  it("removes utm_source and keeps the hash route", () => {
    expect(cleanedUrl(loc("?utm_source=example.com", "#/machines/mac"))).toBe("/#/machines/mac");
  });
  it("keeps non-tracking parameters", () => {
    expect(cleanedUrl(loc("?utm_source=x&utm_medium=y&keep=1&fbclid=abc"))).toBe("/?keep=1#/");
  });
  it("does nothing when the URL is already clean", () => {
    expect(cleanedUrl(loc(""))).toBeNull();
    expect(cleanedUrl(loc("?keep=1"))).toBeNull();
  });
  it("rewrites the address bar without adding a history entry", () => {
    window.history.replaceState(null, "", "/?utm_source=example.com#/activity");
    const before = window.history.length;
    stripTrackingParams();
    expect(window.location.search).toBe("");
    expect(window.location.hash).toBe("#/activity");
    expect(window.history.length).toBe(before);
  });
});
