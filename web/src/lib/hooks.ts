import { useQuery, useMutation } from "@tanstack/react-query";
import {
  fetchKbBases,
  fetchDataSources,
  fetchBudgetUsage,
  fetchAnalyticsStats,
} from "@/lib/api";
import type {
  KbBaseListResponse,
  DataSourceListResponse,
  BudgetUsage,
  AnalyticsStats,
} from "@/lib/api";

// ---------------------------------------------------------------- knowledge

export function useKnowledgeList() {
  return useQuery<KbBaseListResponse>({
    queryKey: ["knowledge-list"],
    queryFn: () => fetchKbBases(),
  });
}

// ---------------------------------------------------------------- sources

export function useSources() {
  return useQuery<DataSourceListResponse>({
    queryKey: ["data-sources"],
    queryFn: () => fetchDataSources(),
  });
}

// ---------------------------------------------------------------- jobs

export interface JobItem {
  id: string;
  name?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
  [key: string]: unknown;
}

export interface JobRunItem {
  id: string;
  job_id: string;
  status?: string;
  started_at?: string;
  finished_at?: string;
  [key: string]: unknown;
}

export function useJobs() {
  return useQuery<JobItem[]>({
    queryKey: ["jobs"],
    queryFn: async () => {
      const res = await fetch("/api/v1/jobs", {
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) throw new Error(`获取 Job 列表失败 HTTP ${res.status}`);
      return (await res.json()) as JobItem[];
    },
  });
}

export function useJobRuns(jobId: string | null) {
  return useQuery<JobRunItem[]>({
    queryKey: ["job-runs", jobId],
    enabled: !!jobId,
    queryFn: async () => {
      if (!jobId) return [];
      const res = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/runs`, {
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) throw new Error(`获取 Job 运行记录失败 HTTP ${res.status}`);
      return (await res.json()) as JobRunItem[];
    },
  });
}

// ---------------------------------------------------------------- analytics stat

export function useAnalyticStat(range: string) {
  return useQuery<AnalyticsStats>({
    queryKey: ["analytics-stats", range],
    queryFn: () => fetchAnalyticsStats(range),
  });
}

// ---------------------------------------------------------------- budget usage

export function useBudgetUsage() {
  return useQuery<BudgetUsage>({
    queryKey: ["budget-usage"],
    queryFn: () => fetchBudgetUsage(),
  });
}

// ---------------------------------------------------------------- export ZIP

export function useExportZIP(sessionId: string) {
  return useMutation({
    mutationFn: async () => {
      const res = await fetch(`/api/v1/export/zip?session_id=${encodeURIComponent(sessionId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) throw new Error(`导出 ZIP 失败 HTTP ${res.status}`);
      return await res.blob();
    },
  });
}
