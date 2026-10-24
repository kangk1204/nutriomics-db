# Archive-only instrumentation: preprocessing, contrasts and FDR are unchanged.
archive_script_argument <- sub("^--file=", "", grep("^--file=", commandArgs(), value=TRUE)[1L])
archive_helper_path <- file.path(dirname(archive_script_argument), "expression_archive_helpers.R")
source(archive_helper_path)
script_argument <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
if (!length(script_argument)) stop("Run as an Rscript file, not an interactive selection.")
source_root <- normalizePath(file.path(dirname(sub("^--file=", "", script_argument[[1L]])), ".."))
project_root <- normalizePath(file.path(source_root, ".."))
r_library <- Sys.getenv("NUTRIGENOMICS_R_LIB", file.path(project_root, "toolchain", "r-library"))
if (dir.exists(r_library)) .libPaths(c(normalizePath(r_library), .libPaths()))

suppressPackageStartupMessages({
  library(limma)
  library(AnnotationDbi)
  library(org.Hs.eg.db)
  library(fgsea)
  library(reactome.db)
  library(jsonlite)
})

args <- commandArgs(trailingOnly = TRUE)
arg_value <- function(name, default) {
  index <- match(name, args)
  if (is.na(index) || index == length(args)) default else args[[index + 1L]]
}

data_root <- normalizePath(arg_value("--data-dir", Sys.getenv("NUTRIGENOMICS_DATA_DIR", "data")), mustWork = TRUE)
output_root <- arg_value("--output-dir", file.path(data_root, "..", "outputs", "gse54325-v1"))
sample_sheet_path <- arg_value(
  "--sample-sheet",
  file.path(data_root, "..", "outputs", "geo-design-audit", "GSE54325", "sample_design.tsv")
)
raw_root <- file.path(data_root, "processed", "geo", "GSE54325", "raw_files")
annotation_path <- file.path(raw_root, "GPL10332_old_annotations.txt.gz")
dir.create(output_root, recursive = TRUE, showWarnings = FALSE)

if (!file.exists(sample_sheet_path) || !file.exists(annotation_path)) {
  stop("GSE54325 audited sample sheet or GPL10332 platform annotation is missing.")
}
sample_info <- read.delim(sample_sheet_path, check.names = FALSE, stringsAsFactors = FALSE)
if (nrow(sample_info) != 7L || anyDuplicated(sample_info$sample_accession)) {
  stop("Expected seven unique GSE54325 GEO samples.")
}
channel_labels <- lapply(sample_info$channel_labels_json, fromJSON, simplifyVector = TRUE)
channel_sources <- lapply(sample_info$channel_source_names_json, fromJSON, simplifyVector = TRUE)
valid_channels <- vapply(seq_len(nrow(sample_info)), function(index) {
  labels <- channel_labels[[index]]
  sources <- channel_sources[[index]]
  identical(unname(labels$label_ch1[[1L]]), "Cy3") &&
    identical(unname(labels$label_ch2[[1L]]), "Cy5") &&
    grepl("beginning|time 0", sources$source_name_ch1[[1L]], ignore.case = TRUE) &&
    grepl("8 weeks", sources$source_name_ch2[[1L]], ignore.case = TRUE)
}, logical(1))
if (!all(valid_channels)) {
  stop("Expected consistent GEO channel metadata: Cy3 = baseline and Cy5 = 8-week sample.")
}
subject_match <- regmatches(sample_info$title, regexec("Volunteer([0-9]+)", sample_info$title, ignore.case = TRUE))
if (any(lengths(subject_match) != 2L)) stop("Could not parse all Volunteer identifiers.")
sample_info$subject_id <- vapply(subject_match, `[[`, character(1), 2L)
if (length(unique(sample_info$subject_id)) != 7L) stop("Expected seven independent participants.")

raw_files <- list.files(raw_root, pattern = "^GSM[0-9]+_Volunteer[0-9]+\\.txt\\.gz$", full.names = TRUE)
gsm_from_file <- sub("_.*$", "", basename(raw_files))
if (length(raw_files) != nrow(sample_info) || anyDuplicated(gsm_from_file)) {
  stop("Expected one raw Agilent Feature Extraction file per GEO sample.")
}
file_index <- match(sample_info$sample_accession, gsm_from_file)
if (anyNA(file_index)) stop("Raw Agilent files and GEO sample IDs do not match one-to-one.")
raw_files <- raw_files[file_index]

inflate_gzip <- function(source_path, destination_path) {
  input <- gzfile(source_path, open = "rb")
  output <- file(destination_path, open = "wb")
  on.exit(close(input), add = TRUE)
  on.exit(close(output), add = TRUE)
  repeat {
    block <- readBin(input, what = "raw", n = 1024L * 1024L)
    if (!length(block)) break
    writeBin(block, output)
  }
}

scratch <- tempfile("gse54325-feature-extraction-")
dir.create(scratch, recursive = TRUE)
feature_files <- file.path(scratch, paste0(sample_info$sample_accession, ".txt"))
for (index in seq_along(raw_files)) inflate_gzip(raw_files[[index]], feature_files[[index]])

rg <- read.maimages(
  files = feature_files,
  source = "agilent",
  names = sample_info$sample_accession,
  columns = c(G = "gProcessedSignal", R = "rProcessedSignal"),
  annotation = c("FeatureNum", "ControlType", "ProbeName", "SystematicName"),
  green.only = FALSE,
  verbose = TRUE
)
unlink(scratch, recursive = TRUE)
if (!identical(colnames(rg$G), sample_info$sample_accession) || any(!is.finite(rg$G)) || any(!is.finite(rg$R))) {
  stop("Agilent processed channel signals did not load as finite, ordered arrays.")
}
if (!all(c("ProbeName", "ControlType") %in% names(rg$genes))) {
  stop("Agilent Feature Extraction annotation columns are incomplete.")
}
control_type <- trimws(as.character(rg$genes$ControlType))
keep <- is.na(control_type) | control_type %in% c("", "0")
if (sum(keep) < 10000L) stop("Too few non-control Agilent probes were identified.")

rg_gene <- rg[keep, , drop = FALSE]
ma <- normalizeWithinArrays(rg_gene, method = "loess")
annotation <- read.delim(
  gzfile(annotation_path),
  skip = 21L,
  check.names = FALSE,
  quote = "",
  comment.char = "",
  stringsAsFactors = FALSE
)
if (!all(c("NAME", "GENE_SYMBOL") %in% names(annotation))) {
  stop("GPL10332 annotation table lacks NAME or GENE_SYMBOL columns.")
}
annotation_index <- match(as.character(ma$genes$ProbeName), as.character(annotation$NAME))
gene_symbols <- trimws(as.character(annotation$GENE_SYMBOL[annotation_index]))
valid_gene <- !is.na(gene_symbols) & nzchar(gene_symbols) & gene_symbols != "---"
if (mean(valid_gene) < 0.4) stop("Fewer than 40% of usable probes map to a GPL10332 gene symbol.")
gene_m <- avereps(ma$M[valid_gene, , drop = FALSE], ID = gene_symbols[valid_gene])
if (nrow(gene_m) < 1000L) stop("Too few unique gene symbols remain after probe aggregation.")

design <- matrix(1, nrow = ncol(gene_m), ncol = 1L)
colnames(design) <- "Cy5_8w_vs_Cy3_baseline"
rownames(design) <- colnames(gene_m)
unmoderated_fit <- lmFit(gene_m, design)
fit <- eBayes(unmoderated_fit, robust = TRUE)
archive_contrasts <- matrix(1, nrow=1L, ncol=1L, dimnames=list(colnames(design), colnames(design)))
model_archive <- save_expression_model_archive(
  gene_m, sample_info, design, archive_contrasts, unmoderated_fit, fit,
  file.path(output_root, "reanalysis_inputs.rds"), contrast_application="identity_untransformed",
  channel_direction=list(Cy3="baseline", Cy5="8 weeks"))
gene_entrez <- mapIds(
  org.Hs.eg.db,
  keys = rownames(gene_m),
  column = "ENTREZID",
  keytype = "SYMBOL",
  multiVals = "first"
)
gene_results <- data.frame(
  feature_type = "gene",
  feature_id = rownames(gene_m),
  feature_label = rownames(gene_m),
  contrast = "Cy5_8w_vs_Cy3_baseline",
  tissue = "peripheral blood leukocytes",
  timepoint = "8w vs baseline",
  intervention = "8-week monomeric and oligomeric flavanol supplementation",
  dose = NA_character_,
  duration = "8 weeks",
  effect_estimate = fit$coefficients[, 1L],
  standard_error = sqrt(fit$s2.post) * fit$stdev.unscaled[, 1L],
  p_value = fit$p.value[, 1L],
  q_value = p.adjust(fit$p.value[, 1L], method = "BH"),
  effect_unit = "log2 Cy5/Cy3 ratio",
  status = "measured",
  n_people = nrow(sample_info),
  n_samples = nrow(sample_info),
  metadata_json = vapply(rownames(gene_m), function(symbol) {
    as.character(toJSON(list(
      entrez_id = unname(gene_entrez[[symbol]]),
      probe_aggregation = "arithmetic mean of non-control GPL10332 probes mapping to this gene symbol",
      signal_columns = list(Cy3_baseline = "gProcessedSignal", Cy5_8week = "rProcessedSignal")
    ), auto_unbox = TRUE, null = "null"))
  }, character(1)),
  stringsAsFactors = FALSE
)

reactome_membership <- AnnotationDbi::toTable(reactomePATHID2EXTID)
membership_columns <- intersect(c("path_id", "pathway_id", "DB_ID"), names(reactome_membership))
membership_gene_columns <- intersect(c("gene_id", "ext_id", "ENTREZID"), names(reactome_membership))
if (!length(membership_columns) || !length(membership_gene_columns)) {
  stop("Unable to identify Reactome pathway membership columns.")
}
path_id_column <- membership_columns[[1L]]
gene_id_column <- membership_gene_columns[[1L]]
membership <- reactome_membership[grepl("^R-HSA-", reactome_membership[[path_id_column]]), , drop = FALSE]
pathway_sets <- split(as.character(membership[[gene_id_column]]), as.character(membership[[path_id_column]]))
pathway_names_table <- AnnotationDbi::toTable(reactomePATHID2NAME)
path_name_id_column <- intersect(c("DB_ID", "path_id", "PATHID"), names(pathway_names_table))[[1L]]
path_name_column <- intersect(c("path_name", "PATHNAME", "name"), names(pathway_names_table))[[1L]]
pathway_names <- setNames(
  as.character(pathway_names_table[[path_name_column]]),
  as.character(pathway_names_table[[path_name_id_column]])
)
valid_entrez <- !is.na(gene_entrez) & nzchar(gene_entrez) & is.finite(fit$t[, 1L])
ranks <- data.frame(entrez = as.character(gene_entrez[valid_entrez]), t = fit$t[valid_entrez, 1L])
ranks <- ranks[order(-abs(ranks$t), ranks$entrez), ]
ranks <- ranks[!duplicated(ranks$entrez), ]
write.table(data.frame(pathway_id=rep(names(pathway_sets),lengths(pathway_sets)),
  entrez_id=unlist(pathway_sets,use.names=FALSE)),file.path(output_root,"reactome_membership.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
write_json(list(database_metadata=AnnotationDbi::metadata(reactome.db),
  release_status="Exact installed annotation snapshot; not relabeled as current Reactome release"),
  file.path(output_root,"reactome_snapshot.json"),auto_unbox=TRUE,pretty=TRUE)
set.seed(20261001L)
fgsea_result <- fgsea(pathways = pathway_sets, stats = sort(setNames(ranks$t, ranks$entrez), decreasing = TRUE), minSize = 10L, maxSize = 500L, nproc = 1L)
if (!nrow(fgsea_result)) stop("No Reactome pathway results were generated.")
fgsea_result$pathway_name <- unname(pathway_names[fgsea_result$pathway])
pathway_results <- data.frame(
  feature_type = "pathway",
  feature_id = fgsea_result$pathway,
  feature_label = fgsea_result$pathway_name,
  contrast = "Cy5_8w_vs_Cy3_baseline",
  tissue = "peripheral blood leukocytes",
  timepoint = "8w vs baseline",
  intervention = "8-week monomeric and oligomeric flavanol supplementation",
  dose = NA_character_,
  duration = "8 weeks",
  effect_estimate = fgsea_result$NES,
  standard_error = NA_real_,
  p_value = fgsea_result$pval,
  q_value = fgsea_result$padj,
  effect_unit = "normalized enrichment score",
  status = "measured",
  n_people = nrow(sample_info),
  n_samples = nrow(sample_info),
  metadata_json = vapply(seq_len(nrow(fgsea_result)), function(index) {
    as.character(toJSON(list(
      pathway_size = fgsea_result$size[[index]],
      leading_edge = paste(fgsea_result$leadingEdge[[index]], collapse = ";"),
      annotation = "Reactome.db from locked Bioconductor environment"
    ), auto_unbox = TRUE))
  }, character(1)),
  stringsAsFactors = FALSE
)

write_gzip_tsv <- function(data, path) {
  connection <- gzfile(path, open = "wt")
  on.exit(close(connection))
  write.table(data, connection, sep = "\t", quote = FALSE, row.names = FALSE, na = "NA")
}
write_gzip_tsv(gene_results, file.path(output_root, "gene_effects.tsv.gz"))
write_gzip_tsv(pathway_results, file.path(output_root, "pathway_effects.tsv.gz"))
write.table(sample_info, file.path(output_root, "sample_metadata.tsv"), sep = "\t", quote = FALSE, row.names = FALSE)
sample_qc <- data.frame(
  sample_accession = colnames(ma$M),
  subject_id = sample_info$subject_id,
  median_M = apply(ma$M, 2L, median, na.rm = TRUE),
  mean_M = colMeans(ma$M, na.rm = TRUE),
  sd_M = apply(ma$M, 2L, sd, na.rm = TRUE)
)
write.table(sample_qc, file.path(output_root, "sample_qc.tsv"), sep = "\t", quote = FALSE, row.names = FALSE)
pdf(file.path(output_root, "agilent_qc.pdf"), width = 9, height = 6)
boxplot(as.data.frame(ma$M), outline = FALSE, las = 2, main = "GSE54325 normalized Cy5/Cy3 log2 ratios")
plot(density(gene_m[, 1L]), main = "Gene-level normalized log ratios", xlab = "log2 Cy5/Cy3 ratio")
for (index in 2:ncol(gene_m)) lines(density(gene_m[, index]), col = rgb(0, 0, 0, 0.2))
dev.off()

sha256_file <- function(path) {
  output <- system2("sha256sum", shQuote(path), stdout = TRUE, stderr = TRUE)
  if (!length(output) || (attr(output, "status") %||% 0L) != 0L) stop(paste("SHA-256 failed for", path))
  strsplit(output[[1L]], "[[:space:]]+")[[1L]][[1L]]
}
`%||%` <- function(value, fallback) if (is.null(value)) fallback else value
script_path <- normalizePath(file.path("scripts", "analyze_gse54325.R"), mustWork = TRUE)
lock_path <- normalizePath(file.path(data_root, "..", "toolchain", "r-environment-linux-64.lock.txt"), mustWork = TRUE)
overlay_lock_path <- normalizePath(file.path(data_root, "..", "toolchain", "r-overlay-lock.txt"), mustWork = TRUE)
input_paths <- c(
  raw_files,
  sample_sheet_path,
  annotation_path,
  file.path(raw_root, "extraction_manifest.json"),
  lock_path,
  overlay_lock_path
)
input_manifest <- lapply(input_paths, function(path) {
  list(path = normalizePath(path, mustWork = TRUE), bytes = file.info(path)$size, sha256 = sha256_file(path))
})
metadata <- list(
  accession = "GSE54325",
  analysis_version = arg_value("--analysis-version", "gse54325-agilent-limma-v1"),
  run_utc = format(Sys.time(), tz = "UTC", usetz = TRUE),
  script_path = script_path,
  script_sha256 = sha256_file(script_path),
  environment_lock_path = lock_path,
  environment_lock_sha256 = sha256_file(lock_path),
  overlay_lock_path = overlay_lock_path,
  overlay_lock_sha256 = sha256_file(overlay_lock_path),
  input_manifest = input_manifest,
  sample_count = nrow(sample_info),
  independent_people = length(unique(sample_info$subject_id)),
  channel_direction = list(Cy3 = "baseline", Cy5 = "8 weeks"),
  normalization = "limma read.maimages of gProcessedSignal/rProcessedSignal; within-array loess; gene-level probe averaging",
  design = "One paired 8-week/baseline log2 ratio per independent participant; moderated intercept test against zero",
  result_files = list("gene_effects.tsv.gz", "pathway_effects.tsv.gz"),
  auxiliary_files = list("sample_metadata.tsv", "sample_qc.tsv", "agilent_qc.pdf", "reanalysis_inputs.rds"),
  model_archive = model_archive,
  helper_path = normalizePath(archive_helper_path), helper_sha256 = expression_archive_sha256(archive_helper_path),
  effect_scope = "Seven-person cohort-level flavanol supplementation effect; not a single-food component or individual prediction",
  software_versions = list(
    R = as.character(getRversion()),
    limma = as.character(packageVersion("limma")),
    preprocessCore = as.character(packageVersion("preprocessCore")),
    fgsea = as.character(packageVersion("fgsea")),
    org.Hs.eg.db = as.character(packageVersion("org.Hs.eg.db")),
    reactome.db = as.character(packageVersion("reactome.db"))
  )
)
write_json(metadata, file.path(output_root, "analysis_metadata.json"), pretty = TRUE, auto_unbox = TRUE)
cat(toJSON(list(
  status = "completed",
  accession = "GSE54325",
  samples = nrow(sample_info),
  independent_people = length(unique(sample_info$subject_id)),
  probes = sum(keep),
  genes = nrow(gene_m),
  gene_effects = nrow(gene_results),
  pathway_effects = nrow(pathway_results)
), pretty = TRUE, auto_unbox = TRUE), "\n")
