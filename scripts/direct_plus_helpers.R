# Bounded-memory DIRECT PLUS helpers. Source has no analysis side effects.
dp_atomic_rds <- function(object, path) {
  temporary <- paste0(path, ".tmp")
  saveRDS(object, temporary, compress=FALSE)
  if (!file.rename(temporary, path)) stop("Atomic RDS installation failed")
}

dp_file_sha <- function(path) {
  if (!file.exists(path)) stop("Required input missing: ", path)
  if (requireNamespace("openssl",quietly=TRUE)) {
    con <- file(path,"rb"); on.exit(close(con))
    # as.character(hash) still carries openssl's S3 hash/sha256 classes.
    # A metadata SHA must be a plain scalar so jsonlite never dispatches asJSON.sha256.
    return(paste0(as.character(openssl::sha256(con)),collapse=""))
  }
  out <- system2("sha256sum", shQuote(path), stdout=TRUE, stderr=TRUE)
  if (!is.null(attr(out, "status")) || !grepl("^[0-9a-f]{64} ", out[1L])) stop("SHA256 failed: ", path)
  strsplit(out[1L], " +")[[1L]][1L]
}

dp_design <- function(person_info, nuisance=character(), label="primary") {
  info <- person_info
  info$arm <- factor(info$arm, levels=c("HDG", "MED", "greenMED"))
  base <- model.matrix(~0+arm, info); colnames(base) <- levels(info$arm)
  if (any(table(info$arm)==0L) || qr(base)$rank!=3L) stop("Primary arms are incomplete")
  C <- matrix(c(0,-1,1, -1,1,0, -1,0,1), nrow=3L,
    dimnames=list(colnames(base), c("greenMED_vs_MED_18month_change", "MED_vs_HDG_18month_change", "greenMED_vs_HDG_18month_change")))
  absent <- nuisance[!nuisance %in% names(info)]
  if (length(absent)) return(list(label=label, status="unavailable_missing_covariates", missing=absent))
  if (length(nuisance) && any(!complete.cases(info[,nuisance,drop=FALSE])))
    return(list(label=label, status="unavailable_missing_covariates", missing=nuisance))
  # Constant nuisance terms cannot alter an arm contrast; omit them explicitly.
  constant <- nuisance[vapply(info[,nuisance,drop=FALSE], function(x) length(unique(x))<2L, logical(1))]
  varying <- setdiff(nuisance, constant)
  extra <- if (length(varying)) model.matrix(reformulate(varying), info)[,-1L,drop=FALSE] else matrix(numeric(),nrow(info),0L)
  X <- cbind(base, extra)
  full_C <- rbind(C, matrix(0, ncol(extra), ncol(C), dimnames=list(colnames(extra),colnames(C))))
  decomposition <- svd(X, nu=0L, nv=ncol(X))
  rank <- sum(decomposition$d > max(dim(X))*max(decomposition$d)*.Machine$double.eps)
  basis <- decomposition$v[,seq_len(rank),drop=FALSE]
  residual <- full_C - basis %*% crossprod(basis, full_C)
  estimable <- colSums(residual^2) < 1e-16
  audit <- list(label=label, full_columns=colnames(X), full_rank=rank,
    full_column_count=ncol(X), residual_df=nrow(X)-rank,
    contrast_estimable=setNames(as.list(estimable),colnames(C)), constant_nuisance_omitted=constant,
    independent_people=nrow(X), biological_unit="individual paired 18-month minus baseline change")
  if (!all(estimable) || nrow(X)-rank<2L) return(c(audit,list(status="unavailable_nonidentifiable")))
  selected <- sort(qr(X)$pivot[seq_len(rank)])
  if (!all(seq_len(3L) %in% selected)) stop("Nuisance reduction would remove an arm coefficient")
  audit$dropped_redundant_nuisance <- colnames(X)[-selected]
  # This reduced basis is used only when all original arm contrasts are estimable.
  c(audit,list(status="estimable_pending_scientific_review", design=X[,selected,drop=FALSE],
               contrasts=full_C[selected,,drop=FALSE]))
}

dp_stream_qc <- function(chunk_paths, sample_info) {
  failure <- rep(NA_real_,nrow(sample_info)); feature_ids <- NULL; seen <- rep(FALSE,nrow(sample_info))
  for (path in chunk_paths) {
    chunk <- readRDS(path); index <- match(chunk$sample_ids, sample_info$sample_id)
    if (anyNA(index) || anyDuplicated(index) || length(index)!=ncol(chunk$beta) ||
        !identical(rownames(chunk$beta),rownames(chunk$m_values)) ||
        !identical(rownames(chunk$beta),rownames(chunk$detected)) ||
        !identical(colnames(chunk$beta),chunk$sample_ids) ||
        !identical(colnames(chunk$m_values),chunk$sample_ids) ||
        !identical(colnames(chunk$detected),chunk$sample_ids) ||
        length(chunk$sample_detection_fail_fraction)!=length(index)) stop("Chunk matrix/sample mapping changed")
    if (is.null(feature_ids)) feature_ids <- rownames(chunk$beta)
    if (!identical(feature_ids,rownames(chunk$beta)) || any(seen[index])) stop("Overlapping/incompatible chunks")
    seen[index] <- TRUE
    failure[index] <- chunk$sample_detection_fail_fraction
    rm(chunk); gc(FALSE)
  }
  if (!all(seen)) stop("Incomplete sample QC values")
  bad <- unique(sample_info$subject_id[failure>.05|!is.finite(failure)])
  included <- !sample_info$subject_id %in% bad
  if (!any(included)) stop("No complete pairs pass sample QC")
  detected <- integer(length(feature_ids)); finite <- rep(TRUE,length(feature_ids))
  for (path in chunk_paths) {
    chunk <- readRDS(path); index <- match(chunk$sample_ids, sample_info$sample_id)
    keep <- included[index]
    detected <- detected + rowSums(chunk$detected[,keep,drop=FALSE],na.rm=TRUE)
    finite <- finite & rowSums(!is.finite(chunk$beta[,keep,drop=FALSE]))==0L &
      rowSums(!is.finite(chunk$m_values[,keep,drop=FALSE]))==0L
    rm(chunk); gc(FALSE)
  }
  list(feature_ids=feature_ids, failure=failure, included=included,
       detected_count=detected, finite=finite, detection_pass=detected>=.95*sum(included))
}

dp_write_tiles <- function(chunk_paths, sample_ids, feature_ids, directory, tile_rows=20000L) {
  if (!length(feature_ids) || tile_rows<1L) stop("Invalid feature/tile count")
  dir.create(directory,recursive=TRUE,showWarnings=FALSE)
  tiles <- split(seq_along(feature_ids), ceiling(seq_along(feature_ids)/tile_rows))
  # This is a new staging directory; never append to files from a previous partial build.
  paths <- lapply(seq_along(tiles),function(k) setNames(file.path(directory,sprintf("tile_%03d_%s.bin",k,c("m","beta"))),c("m","beta")))
  if (any(file.exists(unlist(paths)))) stop("Tile staging directory is not empty")
  seen <- character()
  for (path in chunk_paths) {
    chunk <- readRDS(path); cols <- which(chunk$sample_ids %in% sample_ids)
    rows <- match(feature_ids,rownames(chunk$beta))
    if (anyNA(rows) || any(chunk$sample_ids[cols] %in% seen)) stop("Incompatible/duplicate source matrix columns")
    seen <- c(seen,chunk$sample_ids[cols])
    for (k in seq_along(tiles)) for (scale in c("m","beta")) {
      con <- file(paths[[k]][[scale]],"ab")
      values <- if (scale=="m") chunk$m_values else chunk$beta
      tryCatch(writeBin(as.double(values[rows[tiles[[k]]],cols,drop=FALSE]),con,size=8L,endian="little"),finally=close(con))
    }
    rm(chunk,values); gc(FALSE)
  }
  if (!identical(seen,sample_ids)) stop("Disk tile sample order differs from declared order")
  records <- lapply(seq_along(tiles),function(k) {
    bytes <- length(tiles[[k]])*length(sample_ids)*8
    if (any(file.info(paths[[k]])$size!=bytes)) stop("Incomplete disk matrix tile")
    list(feature_ids=feature_ids[tiles[[k]]], sample_ids=sample_ids, rows=length(tiles[[k]]),
         columns=length(sample_ids), endian="little", storage="column-major float64",
         paths=as.list(paths[[k]]), sha256=as.list(vapply(paths[[k]],dp_file_sha,character(1))))
  })
  records
}

dp_read_tile <- function(tile, scale) {
  path <- tile$paths[[scale]]
  if (file.info(path)$size!=tile$rows*tile$columns*8 || dp_file_sha(path)!=tile$sha256[[scale]]) stop("Disk matrix tile changed")
  con <- file(path,"rb"); on.exit(close(con))
  matrix(readBin(con,"double",n=tile$rows*tile$columns,size=8L,endian="little"),
         nrow=tile$rows,dimnames=list(tile$feature_ids,tile$sample_ids))
}

dp_join_fits <- function(paths) {
  # Unmoderated feature-block fits are joined before ONE eBayes call per scale/model.
  # Never moderate or adjust P values independently inside feature blocks.
  combined <- NULL
  for (path in paths) {
    fit <- readRDS(path)
    if (is.null(combined)) { combined <- fit; next }
    for (key in c("design","contrasts","cov.coefficients","rank","pivot","checkpoint_key"))
      if (!identical(combined[[key]],fit[[key]])) stop("Feature-block model identity differs: ",key)
    for (key in c("coefficients","stdev.unscaled")) combined[[key]] <- rbind(combined[[key]],fit[[key]])
    for (key in c("sigma","df.residual","Amean")) combined[[key]] <- c(combined[[key]],fit[[key]])
    rm(fit); gc(FALSE)
  }
  combined$input_tile_sha256 <- NULL
  combined
}
