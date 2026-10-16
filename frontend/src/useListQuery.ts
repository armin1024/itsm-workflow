import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

const PAGE_SIZES = new Set([20, 50, 100]);

function positiveInt(value: string | null, fallback: number): number {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

export function useListQuery(statusKey: string, defaultStatus: string) {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = positiveInt(searchParams.get("page"), 1);
  const rawSize = positiveInt(searchParams.get("pageSize"), 20);
  const pageSize = PAGE_SIZES.has(rawSize) ? rawSize : 20;
  const keyword = searchParams.get("keyword") || "";
  const status = searchParams.get(statusKey) || defaultStatus;
  const [draftKeyword, setDraftKeyword] = useState(keyword);
  const exactFilters = Object.fromEntries(
    [...searchParams.entries()].filter(
      ([key]) => !["page", "pageSize", "keyword", statusKey].includes(key),
    ),
  );

  useEffect(() => setDraftKeyword(keyword), [keyword]);

  const update = useCallback(
    (values: Record<string, string | number>, replace = false) => {
      const next = new URLSearchParams(searchParams);
      Object.entries(values).forEach(([key, value]) => {
        const text = String(value);
        const isDefault =
          (key === "page" && text === "1") ||
          (key === "pageSize" && text === "20") ||
          (key === statusKey && text === defaultStatus) ||
          (key === "keyword" && !text.trim());
        if (isDefault) next.delete(key);
        else next.set(key, text);
      });
      setSearchParams(next, { replace });
    },
    [defaultStatus, searchParams, setSearchParams, statusKey],
  );

  const commitKeyword = useCallback(
    (value = draftKeyword, replace = false) => {
      update({ keyword: value.trim(), page: 1 }, replace);
    },
    [draftKeyword, update],
  );

  useEffect(() => {
    if (draftKeyword.trim() === keyword) return;
    const timer = window.setTimeout(
      () => commitKeyword(draftKeyword, true),
      350,
    );
    return () => window.clearTimeout(timer);
  }, [commitKeyword, draftKeyword, keyword]);

  return {
    page,
    pageSize,
    keyword,
    status,
    draftKeyword,
    setDraftKeyword,
    commitKeyword: () => commitKeyword(draftKeyword),
    setPage: (value: number) => update({ page: Math.max(1, value) }),
    setPageSize: (value: number) => update({ pageSize: value, page: 1 }),
    setStatus: (value: string) => update({ [statusKey]: value, page: 1 }),
    exactFilters,
    setExactFilter: (key: string, value: string) =>
      update({ [key]: value, page: 1 }),
    clearExactFilter: (key: string) => update({ [key]: "", page: 1 }),
    clear: () => {
      setDraftKeyword("");
      const cleared = Object.fromEntries(
        Object.keys(exactFilters).map((key) => [key, ""]),
      );
      update({ ...cleared, keyword: "", [statusKey]: defaultStatus, page: 1 });
    },
  };
}
