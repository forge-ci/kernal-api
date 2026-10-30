#![feature(rustc_private)]

extern crate rustc_errors;
extern crate rustc_hir;
extern crate rustc_lint;
extern crate rustc_middle;
extern crate rustc_session;
extern crate rustc_span;

use rustc_errors::DiagDecorator;
use rustc_hir::def::{DefKind, Res};
use rustc_hir::def_id::{DefId, LocalDefId, LOCAL_CRATE};
use rustc_hir::{
    AmbigArg, Expr, ExprKind, ImplItem, Item, ItemKind, TraitItem, Ty, TyKind, UseKind,
};
use rustc_lint::{LateContext, LateLintPass, LintContext};
use rustc_middle::ty;
use rustc_span::{FileName, RemapPathScopeComponents, Span};
use std::collections::BTreeMap;
use std::path::Path;

dylint_linting::dylint_library!();

rustc_session::declare_lint! {
    /// ### What it does
    ///
    /// Rejects client references to implementation crates owned by
    /// `kernal-api`. Clients use facade-owned types and operations instead,
    /// which prevents dependency/version drift and backend vocabulary from
    /// escaping into application interfaces.
    ///
    /// Inside `kernal-api` itself the rule is the complementary one: a backend
    /// may be used privately, but naming one in a public type position --
    /// enum variant payload, public field, function parameter or return type,
    /// alias, or bound -- puts backend vocabulary back into the application
    /// interface just as surely as a `pub use` does. An exported `pub use` of an
    /// owned crate is rejected directly, renamed or not: `as` changes only the
    /// spelling, never the backend type a client receives, and no owned crate
    /// -- `running_process` included -- has an approved exception. Implementing
    /// a backend's own trait for an exported type does the same from the other
    /// direction, because the trait has to be in scope to call the method; only
    /// the async mirrors of `std::io` are exempt.
    pub KERNAL_API_BOUNDARY,
    Deny,
    "require systems and async APIs owned by kernal-api to pass through its facades"
}

rustc_session::declare_lint! {
    /// ### What it does
    ///
    /// Rejects client calls to a kernal-api wait that has no time budget when a
    /// bounded `_within` / `_for` replacement exists (#433). A dead or wedged
    /// peer, holder or child turns an unbounded wait into a hang that no caller
    /// can diagnose. The call is matched by its resolved definition, so an
    /// alias import, a UFCS call or a function item is caught as well as a
    /// method call. The facade owner itself is not checked yet; its internal
    /// callers are tracked in `docs/wait-inventory.md`.
    pub KERNAL_API_UNBOUNDED_WAIT,
    Deny,
    "require the bounded form of a kernal-api wait"
}

rustc_session::declare_lint_pass!(KernalApiBoundary => [KERNAL_API_BOUNDARY, KERNAL_API_UNBOUNDED_WAIT]);

#[allow(clippy::no_mangle_with_rust_abi)]
#[unsafe(no_mangle)]
pub fn register_lints(sess: &rustc_session::Session, lint_store: &mut rustc_lint::LintStore) {
    dylint_linting::init_config(sess);
    lint_store.register_lints(&[KERNAL_API_BOUNDARY, KERNAL_API_UNBOUNDED_WAIT]);
    lint_store.register_late_pass(|_| Box::new(KernalApiBoundary));
}

/// The unbounded kernal-api waits a client may not call, as `(defining
/// module, owner type, function, bounded replacement)`. A free function has
/// an empty owner. Every row has a bounded form; see `docs/wait-inventory.md`.
/// Service-lifetime listeners (`accept`, interrupt waits) are deliberately
/// absent.
const UNBOUNDED_WAITS: &[(&str, &str, &str, &str)] = &[
    ("async_engine", "Receiver", "recv", "recv_within"),
    ("async_engine", "Runtime", "run", "run_within"),
    ("async_engine", "Semaphore", "acquire", "acquire_within"),
    (
        "async_engine",
        "Semaphore",
        "acquire_many",
        "acquire_many_within",
    ),
    ("async_engine", "Sender", "send", "send_within"),
    ("async_engine", "TaskGroup", "join_next", "join_next_within"),
    ("async_engine", "UnboundedReceiver", "recv", "recv_within"),
    ("broadcast", "BroadcastReceiver", "recv", "recv_within"),
    (
        "broker_v2",
        "",
        "connect_published",
        "connect_published_within",
    ),
    ("fs", "", "lock_exclusive", "lock_exclusive_for"),
    ("fs", "", "lock_exclusive_owned", "lock_exclusive_owned_for"),
    ("fs", "", "lock_shared", "lock_shared_for"),
    ("fs", "", "lock_shared_owned", "lock_shared_owned_for"),
    ("ipc", "AsyncReadHalf", "read", "read_within"),
    ("ipc", "AsyncReadHalf", "read_exact", "read_exact_within"),
    ("ipc", "AsyncStream", "connect", "connect_within"),
    ("ipc", "AsyncStream", "flush", "flush_within"),
    ("ipc", "AsyncStream", "read", "read_within"),
    ("ipc", "AsyncStream", "read_exact", "read_exact_within"),
    ("ipc", "AsyncStream", "shutdown", "shutdown_within"),
    ("ipc", "AsyncStream", "write_all", "write_all_within"),
    ("ipc", "AsyncWriteHalf", "flush", "flush_within"),
    ("ipc", "AsyncWriteHalf", "shutdown", "shutdown_within"),
    ("ipc", "AsyncWriteHalf", "write_all", "write_all_within"),
    ("ipc", "Stream", "connect", "connect_within"),
    ("kernal_api", "PlatformChild", "wait", "wait_within"),
    (
        "kernal_api",
        "PlatformChild",
        "wait_with_output_bounded",
        "wait_with_output_bounded_within",
    ),
    ("mutex", "Mutex", "blocking_lock", "blocking_lock_within"),
    ("mutex", "Mutex", "lock", "lock_within"),
    ("mutex", "Mutex", "lock_owned", "lock_owned_within"),
    ("process", "DaemonChild", "wait", "wait_within"),
    ("process", "SpawnedChild", "wait", "wait_within"),
    ("rw_lock", "RwLock", "blocking_read", "blocking_read_within"),
    (
        "rw_lock",
        "RwLock",
        "blocking_write",
        "blocking_write_within",
    ),
    ("rw_lock", "RwLock", "read", "read_within"),
    ("rw_lock", "RwLock", "read_owned", "read_owned_within"),
    ("rw_lock", "RwLock", "write", "write_within"),
    ("rw_lock", "RwLock", "write_owned", "write_owned_within"),
    (
        "tauri",
        "WebviewHandle",
        "wait_for_terminal",
        "wait_until_terminal",
    ),
];

/// The raw platform crates -- `libc`, `mach2`, `widestring`, `winapi`,
/// `windows_sys` -- are owned for the same reason as the backends. #77
/// collapsed the per-host bindings into this crate's private HAL
/// (`src/platform_linux*`, `src/platform_macos*`, `src/platform_win*`), so a
/// client that names one of them is speaking host vocabulary the facade
/// exists to absorb, and a `libc::termios` or a `windows_sys` `HANDLE` in a
/// public position here is the same leak as a backend type. Heavy private use
/// inside those trees stays legal: within the facade owner this set is
/// consulted only for exported items and for owned traits implemented on
/// exported types.
const OWNED_IMPLEMENTATION_CRATES: &[&str] = &[
    "addr2line",
    "blake3",
    "console_api",
    "console_subscriber",
    "crash_handler",
    "framehop",
    "globset",
    "interprocess",
    "jwalk",
    "libc",
    "libsqlite3_sys",
    "mach2",
    "memmap2",
    "mimalloc_pprof",
    "notify",
    "pdb_addr2line",
    "portable_pty",
    "reflink_copy",
    "running_process",
    "rusqlite",
    "sysinfo",
    "tokio",
    "widestring",
    "winapi",
    "windows_sys",
];

/// The owned-crate traits a facade type may implement, as `(crate, trait)`.
///
/// These four are the async mirrors of `std::io::{Read, Write, Seek,
/// BufRead}`: shared byte-stream vocabulary that carries no backend
/// semantics. Implementing one says "this is a byte stream", not "this is an
/// interprocess socket", and no facade-owned substitute could interoperate
/// with `tokio::io::copy`, a codec, or any other combinator written against
/// them. Every other trait in the owned set is a backend's own extension
/// point, and implementing one for an exported type publishes the backend's
/// design as this crate's contract. See DYLINT.md.
const FOUNDATIONAL_TRAIT_IMPLS: &[(&str, &str)] = &[
    ("tokio", "AsyncBufRead"),
    ("tokio", "AsyncRead"),
    ("tokio", "AsyncSeek"),
    ("tokio", "AsyncWrite"),
];

impl<'tcx> LateLintPass<'tcx> for KernalApiBoundary {
    fn check_crate(&mut self, cx: &LateContext<'tcx>) {
        if current_package_is_facade_owner() {
            return;
        }
        for dependency in direct_owned_dependencies() {
            cx.opt_span_lint(
                KERNAL_API_BOUNDARY,
                None::<Span>,
                DiagDecorator(move |diag| {
                    diag.primary_message(format!(
                        "direct dependency on `{dependency}` duplicates a kernal-api-owned implementation; depend on kernal-api and use its facade"
                    ));
                }),
            );
        }
    }

    fn check_expr(&mut self, cx: &LateContext<'tcx>, expr: &'tcx Expr<'tcx>) {
        if is_facade_owner(cx) || is_boundary_source(cx, expr.span) {
            return;
        }
        let def_id = match expr.kind {
            ExprKind::MethodCall(..) => cx.typeck_results().type_dependent_def_id(expr.hir_id),
            ExprKind::Path(qpath) => match cx.qpath_res(&qpath, expr.hir_id) {
                Res::Def(_, def_id) => Some(def_id),
                _ => None,
            },
            _ => None,
        };
        if let Some(def_id) = def_id {
            check_def(cx, expr.span, def_id);
            check_unbounded_wait(cx, expr.span, def_id);
        }
    }

    fn check_ty(&mut self, cx: &LateContext<'tcx>, ty: &'tcx Ty<'tcx, AmbigArg>) {
        if is_facade_owner(cx) || is_boundary_source(cx, ty.span) {
            return;
        }
        let TyKind::Path(qpath) = ty.kind else {
            return;
        };
        if let Res::Def(_, def_id) = cx.qpath_res(&qpath, ty.hir_id) {
            check_def(cx, ty.span, def_id);
        }
    }

    fn check_item(&mut self, cx: &LateContext<'tcx>, item: &'tcx Item<'tcx>) {
        if is_facade_owner(cx) {
            check_public_signature(cx, item.owner_id.def_id);
            check_owned_trait_impl(cx, item.owner_id.def_id);
            // A braced list lowers to one item per leaf plus a stem naming only
            // the shared prefix; checking the leaves reports each re-export once.
            if let ItemKind::Use(path, kind) = item.kind {
                if !matches!(kind, UseKind::ListStem) {
                    check_public_reexport(cx, item, path);
                }
            }
            return;
        }
        if is_boundary_source(cx, item.span) {
            return;
        }
        let ItemKind::Use(path, _) = item.kind else {
            return;
        };
        for resolution in path.res.into_iter().flatten() {
            if let Res::Def(_, def_id) = resolution {
                check_def(cx, item.span, def_id);
            }
        }
    }

    fn check_impl_item(&mut self, cx: &LateContext<'tcx>, item: &'tcx ImplItem<'tcx>) {
        if !is_facade_owner(cx) {
            return;
        }
        // A trait implementation restates a signature the trait already fixed,
        // so the private adapters that convert into a backend type are not the
        // facade choosing to speak backend vocabulary. Inherent methods are.
        if matches!(
            cx.tcx.def_kind(cx.tcx.local_parent(item.owner_id.def_id)),
            DefKind::Impl { of_trait: true }
        ) {
            return;
        }
        check_public_signature(cx, item.owner_id.def_id);
    }

    fn check_trait_item(&mut self, cx: &LateContext<'tcx>, item: &'tcx TraitItem<'tcx>) {
        if is_facade_owner(cx) {
            check_public_signature(cx, item.owner_id.def_id);
        }
    }
}

fn current_package_is_facade_owner() -> bool {
    package_is_facade_owner(std::env::var("CARGO_PKG_NAME").ok().as_deref())
}

fn package_is_facade_owner(package_name: Option<&str>) -> bool {
    package_name == Some("kernal-api")
}

/// The facade owner is exempt from the client rule and subject to the public
/// signature rule instead. Cargo's package environment identifies it during an
/// ordinary `cargo dylint` run; the crate name identifies it in this lint's
/// own UI fixtures, where the package environment belongs to the lint.
fn is_facade_owner(cx: &LateContext<'_>) -> bool {
    current_package_is_facade_owner() || cx.tcx.crate_name(LOCAL_CRATE).as_str() == "kernal_api"
}

/// Reject every owned backend crate reachable from a public type position of
/// `def_id`. This is the half of the boundary that a `pub use` grep and a
/// client-side reference check both miss: a backend named in the payload of a
/// public enum variant, a public field, a signature, an alias, or a bound is
/// backend vocabulary a client must speak, whether or not it is re-exported.
fn check_public_signature(cx: &LateContext<'_>, def_id: LocalDefId) {
    if !cx.effective_visibilities.is_exported(def_id) {
        return;
    }
    let tcx = cx.tcx;
    let span = tcx.def_span(def_id);
    let mut leaks = OwnedTypeLeaks::default();
    let has_predicates = match tcx.def_kind(def_id) {
        DefKind::Struct | DefKind::Enum | DefKind::Union => {
            for variant in tcx.adt_def(def_id).variants() {
                for field in &variant.fields {
                    // Enum variant fields inherit the enum's visibility; a
                    // private or `pub(crate)` struct field names nothing a
                    // client can reach.
                    if !tcx.visibility(field.did).is_public() {
                        continue;
                    }
                    leaks.record_ty(
                        cx,
                        tcx.def_span(field.did),
                        tcx.type_of(field.did)
                            .instantiate_identity()
                            .skip_normalization(),
                    );
                }
            }
            true
        }
        DefKind::Fn | DefKind::AssocFn => {
            let signature = tcx.fn_sig(def_id).instantiate_identity();
            for input_or_output in signature.skip_binder().inputs_and_output {
                leaks.record_ty(cx, span, input_or_output);
            }
            true
        }
        DefKind::TyAlias
        | DefKind::Const { .. }
        | DefKind::AssocConst { .. }
        | DefKind::Static { .. } => {
            leaks.record_ty(
                cx,
                span,
                tcx.type_of(def_id)
                    .instantiate_identity()
                    .skip_normalization(),
            );
            true
        }
        DefKind::Trait => true,
        _ => false,
    };
    if has_predicates {
        for (clause, clause_span) in tcx.predicates_of(def_id).predicates {
            if let ty::ClauseKind::Trait(predicate) = clause.kind().skip_binder() {
                leaks.record_def(cx, *clause_span, predicate.trait_ref.def_id);
                for argument in predicate.trait_ref.args {
                    if let Some(argument) = argument.as_type() {
                        leaks.record_ty(cx, *clause_span, argument);
                    }
                }
            }
        }
    }
    leaks.report(cx);
}

/// Reject an exported re-export of an owned crate's item or module.
///
/// A rename (`pub use running_process::StreamKind as CaptureStream`) or a glob
/// resolves to the same backend definition, so clients still receive the
/// backend type and inherit its versioning. Private and `pub(crate)` imports,
/// and `pub use` inside a module no client can reach, publish nothing.
fn check_public_reexport(cx: &LateContext<'_>, item: &Item<'_>, path: &rustc_hir::UsePath<'_>) {
    let def_id = item.owner_id.def_id;
    let tcx = cx.tcx;
    let exported = cx.effective_visibilities.is_exported(def_id)
        || (tcx.visibility(def_id).is_public()
            && cx
                .effective_visibilities
                .is_exported(tcx.local_parent(def_id)));
    if !exported {
        return;
    }
    let mut leaks = OwnedTypeLeaks::default();
    for resolution in path.res.into_iter().flatten() {
        if let Res::Def(_, resolved) = resolution {
            leaks.record_def(cx, item.span, resolved);
        }
    }
    for (crate_name, span) in leaks.found {
        cx.opt_span_lint(
            KERNAL_API_BOUNDARY,
            Some(span),
            DiagDecorator(move |diag| {
                diag.primary_message(format!(
                    "`{crate_name}` is publicly re-exported by kernal-api; renaming it does not change the backend type a client receives -- expose a facade-owned type instead"
                ));
            }),
        );
    }
}

/// Reject an owned backend's own trait implemented for an exported type.
///
/// This is the same coupling the public signature rule catches, arriving from
/// the other direction: the trait has to be in scope for a client to call the
/// method, so the backend is reachable through this crate's surface even
/// though no signature names it. Two shapes stay legal and are not the leak.
/// A private self type publishes nothing, because no client can name the type
/// the impl is attached to. The reverse direction -- a facade-owned trait
/// implemented for a backend type -- is reachable only by a caller that
/// already holds the backend type, so it imposes nothing on one that does not.
fn check_owned_trait_impl(cx: &LateContext<'_>, def_id: LocalDefId) {
    let tcx = cx.tcx;
    if !matches!(tcx.def_kind(def_id), DefKind::Impl { of_trait: true }) {
        return;
    }
    let trait_ref = tcx
        .impl_trait_ref(def_id)
        .instantiate_identity()
        .skip_normalization();
    let Some(crate_name) = owned_crate_name(cx, trait_ref.def_id) else {
        return;
    };
    let trait_name = tcx.item_name(trait_ref.def_id).to_string();
    if FOUNDATIONAL_TRAIT_IMPLS.contains(&(crate_name.as_str(), trait_name.as_str())) {
        return;
    }
    if !names_an_exported_type(cx, trait_ref.self_ty()) {
        return;
    }
    cx.opt_span_lint(
        KERNAL_API_BOUNDARY,
        Some(tcx.def_span(def_id)),
        DiagDecorator(move |diag| {
            diag.primary_message(format!(
                "`{crate_name}::{trait_name}` is implemented for an exported kernal-api type; a client cannot call through this impl without importing the trait from `{crate_name}` -- offer an inherent method or a facade-owned trait instead"
            ));
        }),
    );
}

/// Whether `ty` reaches a type this crate exports, which is what makes the
/// impl attached to it part of the client-visible surface.
fn names_an_exported_type(cx: &LateContext<'_>, ty: ty::Ty<'_>) -> bool {
    ty.walk().any(|argument| {
        let Some(argument) = argument.as_type() else {
            return false;
        };
        let def_id = match argument.kind() {
            ty::Adt(definition, _) => definition.did(),
            ty::Foreign(def_id) => *def_id,
            _ => return false,
        };
        def_id
            .as_local()
            .is_some_and(|def_id| cx.effective_visibilities.is_exported(def_id))
    })
}

/// One diagnostic per owned crate per item, at the first position that named
/// it, so a wide signature does not bury the boundary violation in repeats.
#[derive(Default)]
struct OwnedTypeLeaks {
    found: BTreeMap<String, Span>,
}

impl OwnedTypeLeaks {
    fn record_ty<'tcx>(&mut self, cx: &LateContext<'tcx>, span: Span, ty: ty::Ty<'tcx>) {
        for argument in ty.walk() {
            let Some(argument) = argument.as_type() else {
                continue;
            };
            let def_id = match argument.kind() {
                ty::Adt(definition, _) => definition.did(),
                ty::FnDef(def_id, _) | ty::Closure(def_id, _) | ty::Foreign(def_id) => *def_id,
                ty::Alias(alias) => alias.kind.def_id(),
                _ => continue,
            };
            self.record_def(cx, span, def_id);
        }
    }

    fn record_def(&mut self, cx: &LateContext<'_>, span: Span, def_id: DefId) {
        if let Some(crate_name) = owned_crate_name(cx, def_id) {
            self.found.entry(crate_name).or_insert(span);
        }
    }

    fn report(self, cx: &LateContext<'_>) {
        for (crate_name, span) in self.found {
            cx.opt_span_lint(
                KERNAL_API_BOUNDARY,
                Some(span),
                DiagDecorator(move |diag| {
                    diag.primary_message(format!(
                        "`{crate_name}` appears in a public kernal-api type position; a client that names this type speaks backend vocabulary -- expose a facade-owned type instead"
                    ));
                }),
            );
        }
    }
}

/// The owned implementation crate `def_id` belongs to, if any.
fn owned_crate_name(cx: &LateContext<'_>, def_id: DefId) -> Option<String> {
    if def_id.is_local() {
        return None;
    }
    let crate_name = cx.tcx.crate_name(def_id.krate).as_str().to_string();
    OWNED_IMPLEMENTATION_CRATES
        .contains(&crate_name.as_str())
        .then_some(crate_name)
}

/// Read the current client package manifest as well as its resolved HIR uses.
fn direct_owned_dependencies() -> Vec<String> {
    let Some(manifest_dir) = std::env::var_os("CARGO_MANIFEST_DIR") else {
        return Vec::new();
    };
    let Ok(source) = std::fs::read_to_string(Path::new(&manifest_dir).join("Cargo.toml")) else {
        return Vec::new();
    };
    direct_owned_dependencies_in(&source)
}

fn direct_owned_dependencies_in(source: &str) -> Vec<String> {
    let Ok(manifest) = toml::from_str::<toml::Value>(source) else {
        return Vec::new();
    };
    if manifest
        .get("package")
        .and_then(|package| package.get("name"))
        .and_then(toml::Value::as_str)
        == Some("kernal-api")
    {
        return Vec::new();
    }

    let mut found = Vec::new();
    collect_owned_dependency_tables(&manifest, &mut found);
    found.sort();
    found.dedup();
    found
}

fn collect_owned_dependency_tables(value: &toml::Value, found: &mut Vec<String>) {
    let Some(table) = value.as_table() else {
        return;
    };
    for (key, value) in table {
        if matches!(
            key.as_str(),
            "dependencies" | "dev-dependencies" | "build-dependencies"
        ) {
            if let Some(dependencies) = value.as_table() {
                for (alias, declaration) in dependencies {
                    let package = declaration
                        .get("package")
                        .and_then(toml::Value::as_str)
                        .unwrap_or(alias);
                    let normalized = package.replace('-', "_");
                    if OWNED_IMPLEMENTATION_CRATES.contains(&normalized.as_str()) {
                        found.push(package.to_owned());
                    }
                }
            }
        } else {
            collect_owned_dependency_tables(value, found);
        }
    }
}

fn is_boundary_source(cx: &LateContext<'_>, span: Span) -> bool {
    let filename = match cx.sess().source_map().span_to_filename(span) {
        FileName::Real(real_filename) => real_filename
            .local_path()
            .map(|path| path.to_string_lossy().into_owned())
            .unwrap_or_else(|| {
                real_filename
                    .path(RemapPathScopeComponents::DIAGNOSTICS)
                    .to_string_lossy()
                    .into_owned()
            }),
        filename => filename
            .display(RemapPathScopeComponents::DIAGNOSTICS)
            .to_string(),
    };
    filename.replace('\\', "/").contains("/kernal-api/src/")
}

/// Reject a client call that resolves to an unbounded kernal-api wait.
fn check_unbounded_wait(cx: &LateContext<'_>, span: Span, def_id: DefId) {
    let Some((owner, function, bounded)) = unbounded_wait(cx, def_id) else {
        return;
    };
    let called = if owner.is_empty() {
        function.to_owned()
    } else {
        format!("{owner}::{function}")
    };
    cx.opt_span_lint(
        KERNAL_API_UNBOUNDED_WAIT,
        Some(span),
        DiagDecorator(move |diag| {
            diag.primary_message(format!(
                "`{called}` can wait forever; call `{bounded}` with a time budget instead"
            ));
        }),
    );
}

/// The `UNBOUNDED_WAITS` row `def_id` resolves to, if it is one.
fn unbounded_wait(
    cx: &LateContext<'_>,
    def_id: DefId,
) -> Option<(&'static str, &'static str, &'static str)> {
    let tcx = cx.tcx;
    if def_id.is_local() || tcx.crate_name(def_id.krate).as_str() != "kernal_api" {
        return None;
    }
    let (owner, module) = match tcx.def_kind(def_id) {
        DefKind::Fn => (String::new(), tcx.parent(def_id)),
        DefKind::AssocFn => {
            let parent = tcx.parent(def_id);
            if !matches!(tcx.def_kind(parent), DefKind::Impl { of_trait: false }) {
                return None;
            }
            let self_ty = tcx
                .type_of(parent)
                .instantiate_identity()
                .skip_normalization();
            let ty::Adt(definition, _) = self_ty.kind() else {
                return None;
            };
            let did = definition.did();
            (tcx.item_name(did).to_string(), tcx.parent(did))
        }
        _ => return None,
    };
    let module = if module.is_crate_root() {
        tcx.crate_name(module.krate).to_string()
    } else {
        tcx.item_name(module).to_string()
    };
    let function = tcx.item_name(def_id);
    UNBOUNDED_WAITS
        .iter()
        .find(|(row_module, row_owner, row_function, _)| {
            *row_module == module && *row_owner == owner && *row_function == function.as_str()
        })
        .map(|(_, row_owner, row_function, bounded)| (*row_owner, *row_function, *bounded))
}

fn check_def(cx: &LateContext<'_>, span: Span, def_id: DefId) {
    let Some(crate_name) = owned_crate_name(cx, def_id) else {
        return;
    };
    cx.opt_span_lint(
        KERNAL_API_BOUNDARY,
        Some(span),
        DiagDecorator(move |diag| {
            diag.primary_message(format!(
                "direct `{crate_name}` use bypasses the kernal-api facade; use a kernal_api-owned type or operation"
            ));
        }),
    );
}

#[test]
fn ui() {
    dylint_testing::ui_test(env!("CARGO_PKG_NAME"), "ui");
}

#[test]
fn the_owned_crate_set_is_sorted_and_unique() {
    assert!(OWNED_IMPLEMENTATION_CRATES
        .windows(2)
        .all(|pair| pair[0] < pair[1]));
}

#[test]
fn the_unbounded_wait_set_is_sorted_and_unique() {
    assert!(UNBOUNDED_WAITS.windows(2).all(|pair| pair[0] < pair[1]));
}

#[test]
fn the_foundational_trait_set_is_sorted_and_owned() {
    assert!(FOUNDATIONAL_TRAIT_IMPLS
        .windows(2)
        .all(|pair| pair[0] < pair[1]));
    // An exemption for a crate outside the owned set would never be reached.
    assert!(FOUNDATIONAL_TRAIT_IMPLS
        .iter()
        .all(|(crate_name, _)| OWNED_IMPLEMENTATION_CRATES.contains(crate_name)));
}

#[test]
fn manifest_scan_finds_normal_aliased_target_and_test_dependencies() {
    let manifest = r#"
        [package]
        name = "client"

        [dependencies]
        tokio = "1"
        async_backend = { package = "console-subscriber", version = "0.5" }
        process_backend = { package = "running-process", version = "4" }
        rusqlite = "0.40"

        [target.'cfg(windows)'.dev-dependencies]
        portable-pty = "0.9"
        sqlite_sys = { package = "libsqlite3-sys", version = "0.35" }
    "#;
    toml::from_str::<toml::Value>(manifest)
        .expect("the client manifest fixture must be valid TOML");
    assert_eq!(
        direct_owned_dependencies_in(manifest),
        [
            "console-subscriber",
            "libsqlite3-sys",
            "portable-pty",
            "running-process",
            "rusqlite",
            "tokio"
        ]
    );
}

#[test]
fn manifest_scan_exempts_the_facade_owner() {
    assert!(direct_owned_dependencies_in(
        r#"[package]
name = "kernal-api"
[dependencies]
tokio = "1"
"#
    )
    .is_empty());
}

#[test]
fn package_identity_exempts_only_the_facade_owner() {
    assert!(package_is_facade_owner(Some("kernal-api")));
    assert!(!package_is_facade_owner(Some("kernal_api_boundary")));
    assert!(!package_is_facade_owner(None));
}
