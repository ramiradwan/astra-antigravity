/** Component-only data. Never place this snapshot in the parent transcript. */
export interface UiArtifact {
  artifact_id?: string;
  name: string;
  bytes?: number;
  mime_type?: string;
  sha256?: string;
  export_available?: boolean;
  download_available?: boolean;
  download_origin?: string;
  /** Legacy metadata only; current components never navigate to this URL. */
  download_url?: string;
  reason?: string;
}

export interface UiSnapshot {
  version: 1;
  delegation_id?: string;
  request_id?:string;
  revision: string;
  status: string;
  terminal: boolean;
  summary: string;
  updated_at: string;
  task_summary?: string;
  workspace_label?: string;
  selected_model?: string;
  setup_url?: string;
  permissions?: { can_write_tasks: boolean };
  connection: { state: "online" | "offline" | "expired" | "revoked" | "unknown" | "setup_required"; message?: string; setup_url?: string };
  notifications: { active: boolean; message?: string };
  decision?: { summary: string; opus_available?: boolean };
  outcome?: string;
  worker_checks?: string[];
  artifacts?: UiArtifact[];
}

export interface UiArtifactContent {
  id: string;
  bytes: number;
  sha256: string;
  encoding: "base64";
  content: string;
}

export interface UiOperation {
  operation_id:string; computer_id?:string; action:string;
  state:"prepared"|"pending"|"completed"|"failed"|"revoked"|"unconfirmed";
  summary:string; updated_at:string; confirmation_token?:string; expires_at?:string;
}
export interface UiControlSnapshot {
  version:1; revision:string; updated_at:string;
  permissions:{can_manage:boolean;can_delete_account:boolean;can_write_tasks:boolean};
  computers:Array<{id:string;label:string;state:"online"|"offline"|"paused"|"expired"|"revoked";paused:boolean;
    approval_mode:"until_revoked"|"legacy_30_day";setup_only:boolean;accounts?:Partial<Record<"codex"|"agy"|"git",{installed:boolean;status:"ready"|"sign_in_required"|"missing"|"unknown";summary?:string}>>;expires_at?:string;companion_version?:string;
    capabilities:string[];allowed_actions:string[];active_task_count:number;pending_task_count:number;
    workspaces:Array<{id:string;label:string}>;startup_enabled?:boolean;automatic_updates_enabled?:boolean;update_status?:string;uninstall_status?:string}>;
  tasks:Array<{delegation_id:string;task_summary:string;status:string;terminal:boolean;workspace_label?:string;computer_id:string;updated_at:string}>;
  settings:{preferred_workspace_id?:string;setup_url:string;setup_command?:string;installer_url?:string;install_unavailable_message?:string};
  receipts:UiOperation[];account:{export_available:boolean;deletion_requires_confirmation:boolean};
}
