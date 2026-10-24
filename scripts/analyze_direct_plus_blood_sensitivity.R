# DIRECT PLUS v3: separate reference-based cell-composition sensitivity.
# Function definitions can be sourced for small tests without loading IDAT/minfi.
dpb_error <- function(message, code="failed") {
  stop(structure(list(message=message,call=NULL,code=code),class=c("dpb_error","error","condition")))
}

dpb_arg <- function(args,name,default=NULL) {
  at<-match(name,args);if(is.na(at)||at==length(args))default else args[[at+1L]]
}

dpb_mem_available <- function(lines=readLines("/proc/meminfo",warn=FALSE)) {
  value<-grep("^MemAvailable:[[:space:]]+[0-9]+[[:space:]]+kB$",lines,value=TRUE)
  if(length(value)!=1L)dpb_error("MemAvailable unavailable; resource safety cannot be established","resource_unavailable")
  as.numeric(strsplit(trimws(value),"[[:space:]]+")[[1L]][2L])*1024/1024^3
}

dpb_memory_gate <- function(available,working=48,reserve=24,native_headroom=4) {
  if(any(!is.finite(c(available,working,reserve,native_headroom)))||working<=0||reserve<24||native_headroom<4)
    dpb_error("Invalid memory budget; >=24 GiB reserve and >=4 GiB native headroom required","resource_unavailable")
  required<-working+reserve+native_headroom
  if(available<required)dpb_error(sprintf("Insufficient MemAvailable %.2f GiB; need %.2f GiB (working %.2f, reserve %.2f, native %.2f)",available,required,working,reserve,native_headroom),"resource_insufficient")
  list(mem_available_gib=available,working_budget_gib=working,reserve_gib=reserve,native_headroom_gib=native_headroom)
}

dpb_close_counts <- function(counts,sample_ids,tolerance=1e-12) {
  types<-c("CD8T","CD4T","NK","Bcell","Mono","Gran")
  if(!is.matrix(counts)||!is.numeric(counts)||!setequal(colnames(counts),types)||
     anyDuplicated(rownames(counts))||!setequal(rownames(counts),sample_ids)||
     any(!is.finite(counts))||any(counts< -tolerance))dpb_error("Cell estimate identity/range incomplete or invalid","invalid_cell_estimates")
  counts<-counts[match(sample_ids,rownames(counts)),types,drop=FALSE]
  sums<-rowSums(counts)
  if(any(sums<=0))dpb_error("Nonpositive sum of reference projections","invalid_cell_estimates")
  negative<-sum(counts<0);minimum<-min(counts);counts[counts<0]<-0
  closed<-counts/rowSums(counts)
  if(any(!is.finite(closed))||max(abs(rowSums(closed)-1))>1e-12||any(closed<0|closed>1))
    dpb_error("Relative composition closure failed","invalid_cell_estimates")
  list(closed=closed,raw_sum_range=range(sums),roundoff_negative_count=negative,raw_minimum=minimum)
}

dpb_cell_design <- function(person_info,sample_info,closed_counts) {
  if(anyDuplicated(paste(sample_info$subject_id,sample_info$time_months))||
     anyDuplicated(person_info$subject_id))dpb_error("Ambiguous person/time mapping","sample_mapping_error")
  people<-as.character(person_info$subject_id)
  key<-paste(sample_info$subject_id,sample_info$time_months)
  baseline<-match(paste(people,0),key);followup<-match(paste(people,18),key)
  if(anyNA(c(baseline,followup)))dpb_error("Incomplete paired sample mapping","sample_mapping_error")
  if(!identical(rownames(closed_counts),as.character(sample_info$sample_id)))dpb_error("Cell count sample order differs","sample_mapping_error")
  types<-c("CD8T","CD4T","NK","Bcell","Mono") # Gran is the omitted reference.
  delta<-closed_counts[followup,types,drop=FALSE]-closed_counts[baseline,types,drop=FALSE]
  colnames(delta)<-paste0("delta_",types);rownames(delta)<-people
  info<-person_info;rownames(info)<-people
  for(k in seq_along(types))info[[colnames(delta)[k]]]<-delta[,k]
  model<-dp_design(info,colnames(delta),"blood_composition_closed")
  list(model=model,person_info=info,delta_counts=delta,baseline=baseline,followup=followup)
}

dpb_main <- function(args,script,output) {
  Sys.setenv(OMP_NUM_THREADS="1",OPENBLAS_NUM_THREADS="1",MKL_NUM_THREADS="1")
  helper<-file.path(dirname(script),"direct_plus_helpers.R");source(helper)
  suppressPackageStartupMessages({library(minfi);library(limma);library(jsonlite);library(FlowSorted.Blood.450k)})
  root<-normalizePath(dpb_arg(args,"--root",""),mustWork=TRUE)
  input<-normalizePath(dpb_arg(args,"--input-dir",""),mustWork=TRUE)
  lock<-normalizePath(dpb_arg(args,"--environment-lock",""),mustWork=TRUE)
  vector_cap<-as.numeric(dpb_arg(args,"--vector-cap-gib","48"))
  reserve<-as.numeric(dpb_arg(args,"--reserve-gib","24"))
  if(!is.finite(vector_cap)||vector_cap<=0||vector_cap>48)dpb_error("Vector cap must be >0 and <=48 GiB","resource_unavailable")
  actual_cap<-mem.maxVSize(vector_cap*1024)
  if(!is.finite(actual_cap)||actual_cap>48*1024)dpb_error("R vector memory cap was not enforced","resource_unavailable")
  resource<-function(stage,working) {
    gate<-dpb_memory_gate(dpb_mem_available(),working,reserve)
    proc<-readLines("/proc/self/status",warn=FALSE)
    gate$stage<-stage;gate$utc<-format(Sys.time(),tz="UTC",usetz=TRUE)
    gate$process_elapsed_seconds<-unname(proc.time()[3L]);gate$r_vector_cap_gib<-actual_cap/1024
    gate$process_memory<-grep("^(VmRSS|VmHWM|VmSize):",proc,value=TRUE)
    gate$reserve_scope<-"Stage prechecks; external launcher must monitor process-group RSS and MemAvailable continuously. R vector cap is not a total RSS limit."
    cat(toJSON(gate,auto_unbox=TRUE),"\n",file=file.path(output,"resource_events.jsonl"),append=TRUE)
    invisible(gate)
  }
  metadata_path<-file.path(input,"analysis_metadata.json")
  parent<-fromJSON(metadata_path,simplifyVector=FALSE)
  if(parent$accession!="E-MTAB-12527"||!parent$analysis_version%in%c("direct-plus-ssnoob-limma-between-arm-v3","direct-plus-ssnoob-limma-between-arm-v4"))
    dpb_error("This adapter requires actual DIRECT PLUS v3/v4 input artifacts with matching helper/environment hashes","input_version_mismatch")
  if(!identical(parent$environment_lock_sha256,dp_file_sha(lock)))dpb_error("Sensitivity environment lock differs from actual parent","input_hash_mismatch")
  for(p in c("minfi","limma"))if(!identical(parent$package_versions[[p]],as.character(packageVersion(p))))
    dpb_error("Sensitivity core package differs from actual parent","input_hash_mismatch")
  if(!identical(parent$helper_sha256,dp_file_sha(helper)))dpb_error("Sensitivity shared helper differs from parent","input_hash_mismatch")
  paired_path<-file.path(input,"paired_change_inputs.rds");paired<-readRDS(paired_path)
  if(is.null(paired$models$primary$design)||is.null(paired$matrix_manifest_sha256)||is.null(paired$sample_info))
    dpb_error("Actual v3 model/matrix artifacts missing","missing_model_artifacts")
  tile_path<-paired$matrix_manifest_path
  if(dp_file_sha(tile_path)!=paired$matrix_manifest_sha256)dpb_error("Parent matrix manifest changed","input_hash_mismatch")
  tile_record<-readRDS(tile_path)
  if(!identical(tile_record$checkpoint_key,paired$checkpoint_key))dpb_error("Parent checkpoint key mismatch","input_hash_mismatch")
  sample_info<-paired$sample_info
  if(nrow(sample_info)!=512L||length(unique(sample_info$subject_id))!=256L||
     anyDuplicated(sample_info$sample_id)||anyDuplicated(paste(sample_info$subject_id,sample_info$time_months))||
     !setequal(sample_info$time_months,c(0L,18L))||any(table(sample_info$subject_id,sample_info$time_months)!=1L))
    dpb_error("Full 256-person/512-array source mapping is required","sample_mapping_error")
  if(!identical(as.character(sample_info$sample_id[sample_info$included]),tile_record$sample_ids))
    dpb_error("Parent included sample order differs from actual tiles","sample_mapping_error")
  raw<-file.path(root,"raw","E-MTAB-12527")
  resolved_path<-file.path(raw,"resolved_files.json");resolved<-fromJSON(resolved_path)
  raw_listing<-resolved$files[resolved$files$section=="raw-data",]
  acquisition_path<-file.path(raw,"acquisition_status.json");acquisition<-fromJSON(acquisition_path)
  receipt_path<-file.path(raw,"raw_receipts.json")
  if(acquisition$status!="raw_complete_local_sha256_verified"||acquisition$source_identity!=resolved$source_identity||
     dp_file_sha(receipt_path)!=acquisition$raw_receipts_sha256)dpb_error("Raw acquisition is incomplete or changed","input_hash_mismatch")
  annotation_path<-file.path(input,"probe_annotation.rds")
  for(path in c(resolved_path,receipt_path,annotation_path)) {
    matches<-Filter(function(x)identical(basename(x$path),basename(path)),parent$input_manifest)
    if(length(matches)!=1L||!identical(matches[[1L]]$sha256,dp_file_sha(path)))
      dpb_error("Actual parent-declared source/annotation hash differs","input_hash_mismatch")
  }
  receipts<-fromJSON(receipt_path)
  bases<-file.path(raw,"idat",sample_info$base)
  raw_files<-as.vector(outer(bases,c("_Grn.idat","_Red.idat"),paste0))
  if(nrow(raw_listing)!=1024L||nrow(receipts)!=1024L||anyDuplicated(raw_listing$name)||anyDuplicated(basename(receipts$path))||
     !setequal(basename(raw_files),raw_listing$name)||!setequal(basename(raw_files),basename(receipts$path)))
    dpb_error("Exact 1024-channel raw set is incomplete","input_hash_mismatch")
  expected_size<-raw_listing$size[match(basename(raw_files),raw_listing$name)]
  expected_sha<-receipts$sha256[match(basename(raw_files),basename(receipts$path))]
  if(anyNA(file.info(raw_files)$size)||any(file.info(raw_files)$size!=expected_size))dpb_error("Raw byte-size validation failed","input_hash_mismatch")
  current_raw_sha<-vapply(raw_files,dp_file_sha,character(1))
  if(any(current_raw_sha!=expected_sha))dpb_error("Raw source SHA256 validation failed","input_hash_mismatch")
  raw_stat<-file.info(raw_files)[,c("size","mtime","ctime"),drop=FALSE]
  reference_dir<-system.file(package="FlowSorted.Blood.450k")
  reference_files<-c(file.path(reference_dir,"DESCRIPTION"),list.files(file.path(reference_dir,"data"),full.names=TRUE,recursive=TRUE))
  if(length(reference_files)<2L)dpb_error("Sorted adult reference data files are unavailable","missing_reference")
  source_text<-paste(c(capture.output(dput(formals(minfi::estimateCellCounts))),deparse(body(minfi::estimateCellCounts))),collapse="\n")
  source_path<-file.path(output,"estimateCellCounts_executed_function.txt")
  if(file.exists(source_path)&&paste(readLines(source_path,warn=FALSE),collapse="\n")!=source_text)dpb_error("Executed reference function changed","input_hash_mismatch")
  if(!file.exists(source_path))writeLines(source_text,source_path,useBytes=TRUE)
  packages<-c("minfi","limma","jsonlite","statmod","FlowSorted.Blood.450k")
  versions<-setNames(lapply(packages,function(p)as.character(packageVersion(p))),packages)
  file_record<-function(p)list(path=normalizePath(p),sha256=dp_file_sha(p),bytes=file.info(p)$size)
  identity<-list(script=file_record(script),helper=file_record(helper),R=as.character(getRversion()),packages=versions,
    parent_metadata=file_record(metadata_path),parent_inputs=file_record(paired_path),matrix_manifest=file_record(tile_path),parent_annotation=file_record(annotation_path),
    environment_lock=file_record(lock),source_listing=file_record(resolved_path),raw_receipts=file_record(receipt_path),
    executed_reference_function=file_record(source_path),reference_files=lapply(reference_files,file_record),
    source_arrays=512L,source_people=256L,normalization_scope="One complete-cohort estimateCellCounts call jointly processing all 512 user arrays plus sorted adult 450K reference; no independently normalized chunks")
  identity_path<-file.path(output,"cell_estimation_identity.json")
  canonical_json<-function(x)toJSON(x,auto_unbox=TRUE,null="null",digits=NA)
  if(file.exists(identity_path)&&!identical(canonical_json(fromJSON(identity_path,simplifyVector=FALSE)),canonical_json(identity)))dpb_error("Cell-estimation source/environment identity changed; use new output","input_hash_mismatch")
  if(!file.exists(identity_path))write_json(identity,identity_path,auto_unbox=TRUE,pretty=TRUE)
  identity_sha<-dp_file_sha(identity_path)
  counts_path<-file.path(output,"full_cohort_estimated_counts.rds")
  counts_receipt_path<-file.path(output,"full_cohort_estimated_counts_receipt.json")
  if(file.exists(counts_path)||file.exists(counts_receipt_path)) {
    if(!file.exists(counts_path)||!file.exists(counts_receipt_path))dpb_error("Partial count checkpoint; preserve and explicitly recover stage","incomplete_checkpoint")
    receipt<-fromJSON(counts_receipt_path)
    if(receipt$identity_sha256!=identity_sha||receipt$counts_sha256!=dp_file_sha(counts_path))dpb_error("Cell count checkpoint changed","input_hash_mismatch")
    cached<-readRDS(counts_path)
    if(!identical(cached$source_sample_ids,as.character(sample_info$sample_id))||!identical(cached$identity_sha256,identity_sha))dpb_error("Cell count checkpoint sample order/identity changed","sample_mapping_error")
    counts<-cached$counts;rm(cached);resource("resumed_full_cohort_counts",6)
  } else {
    resource("before_full_cohort_raw_load",vector_cap)
    targets<-data.frame(Sample_Name=sample_info$sample_id,Basename=bases,stringsAsFactors=FALSE)
    rg<-read.metharray.exp(targets=targets,extended=FALSE,force=FALSE,verbose=TRUE)
    if(!identical(sampleNames(rg),basename(targets$Basename)))dpb_error("minfi raw array order changed","sample_mapping_error")
    sampleNames(rg)<-sample_info$sample_id
    resource("before_joint_reference_normalization",32)
    counts<-estimateCellCounts(rg,compositeCellType="Blood",referencePlatform="IlluminaHumanMethylation450k",
      processMethod="auto",probeSelect="auto",returnAll=FALSE,meanPlot=FALSE,verbose=TRUE)
    rm(rg,targets);gc(FALSE)
    resource("after_joint_estimation_raw_released",6)
    if(!identical(file.info(raw_files)[,c("size","mtime","ctime"),drop=FALSE],raw_stat))dpb_error("Raw files changed during joint estimation","input_hash_mismatch")
    dp_atomic_rds(list(counts=counts,source_sample_ids=as.character(sample_info$sample_id),identity_sha256=identity_sha),counts_path)
    write_json(list(identity_sha256=identity_sha,counts_sha256=dp_file_sha(counts_path),source_arrays=512L,
      estimation_calls=1L,normalization_scope=identity$normalization_scope),counts_receipt_path,auto_unbox=TRUE,pretty=TRUE)
  }
  closure<-dpb_close_counts(counts,as.character(sample_info$sample_id))
  counts<-counts[match(sample_info$sample_id,rownames(counts)),,drop=FALSE]
  write.table(data.frame(sample_id=sample_info$sample_id,counts),file.path(output,"estimated_cell_proportions.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
  write.table(data.frame(sample_id=sample_info$sample_id,closure$closed),file.path(output,"closed_cell_proportions.tsv"),sep="\t",quote=FALSE,row.names=FALSE)
  cell_model<-dpb_cell_design(paired$person_info,sample_info,closure$closed)
  model<-cell_model$model
  write_json(model[setdiff(names(model),c("design","contrasts"))],file.path(output,"cell_design_audit.json"),auto_unbox=TRUE,pretty=TRUE)
  if(model$status!="estimable_pending_scientific_review")dpb_error("Cell-adjusted arm contrasts are not estimable; no result released","not_estimable")
  selected_info<-sample_info[match(tile_record$sample_ids,sample_info$sample_id),]
  people<-as.character(paired$person_info$subject_id)
  baseline<-match(sample_info$sample_id[cell_model$baseline],tile_record$sample_ids)
  followup<-match(sample_info$sample_id[cell_model$followup],tile_record$sample_ids)
  if(anyNA(c(baseline,followup)))dpb_error("Cell model and included assay tiles disagree","sample_mapping_error")
  dp_atomic_rds(list(model=model,person_info=cell_model$person_info,delta_cell_fractions=cell_model$delta_counts,
    included_sample_info=selected_info,baseline_columns=baseline,followup_columns=followup,
    parent_inputs=file_record(paired_path),matrix_manifest=file_record(tile_path),identity_sha256=identity_sha),file.path(output,"design_and_contrasts.rds"))
  blocks<-file.path(output,"unmoderated_fit_blocks");dir.create(blocks,showWarnings=FALSE)
  for(k in seq_along(tile_record$tiles))for(scale in c("m","beta")) {
    resource(paste0("feature_block_",k,"_",scale),6)
    values<-dp_read_tile(tile_record$tiles[[k]],scale)
    delta<-values[,followup,drop=FALSE]-values[,baseline,drop=FALSE];colnames(delta)<-people
    rm(values);gc(FALSE)
    fit<-contrasts.fit(lmFit(delta,model$design),model$contrasts)
    fit$checkpoint_key<-identity_sha
    dp_atomic_rds(fit,file.path(blocks,sprintf("%s_%03d.rds",scale,k)))
    rm(fit,delta);gc(FALSE)
  }
  resource("before_global_empirical_bayes",6)
  fit_m<-eBayes(dp_join_fits(file.path(blocks,sprintf("m_%03d.rds",seq_along(tile_record$tiles)))),robust=TRUE)
  fit_beta<-eBayes(dp_join_fits(file.path(blocks,sprintf("beta_%03d.rds",seq_along(tile_record$tiles)))),robust=TRUE)
  features<-tile_record$feature_ids
  if(!identical(rownames(fit_m$coefficients),features)||!identical(rownames(fit_beta$coefficients),features))dpb_error("Global fit feature order changed","input_hash_mismatch")
  fit_m$matrix_manifest_sha256<-paired$matrix_manifest_sha256;fit_beta$matrix_manifest_sha256<-paired$matrix_manifest_sha256
  dp_atomic_rds(fit_m,file.path(output,"fitted_m_global_ebayes.rds"));dp_atomic_rds(fit_beta,file.path(output,"fitted_beta_global_ebayes.rds"))
  if(dp_file_sha(annotation_path)!=identity$parent_annotation$sha256)dpb_error("Parent annotation changed during sensitivity","input_hash_mismatch")
  annotation<-readRDS(annotation_path);annotation<-annotation[match(features,rownames(annotation)),,drop=FALSE]
  if(!identical(rownames(annotation),features))dpb_error("Parent probe annotation missing","input_hash_mismatch")
  genes<-as.character(annotation$UCSC_RefGene_Name)
  q<-matrix(p.adjust(as.vector(fit_m$p.value),"BH"),nrow=length(features),dimnames=dimnames(fit_m$p.value))
  result_path<-file.path(output,"cpg_effects.tsv.gz");con<-gzfile(result_path,"wt")
  tryCatch(for(k in seq_len(ncol(model$contrasts))) {
    se<-fit_beta$stdev.unscaled[,k]*sqrt(fit_beta$s2.post);critical<-qt(.975,fit_beta$df.total)
    arms<-c("HDG","MED","greenMED")[model$contrasts[c("HDG","MED","greenMED"),k]!=0]
    n<-sum(cell_model$person_info$arm%in%arms)
    results<-data.frame(feature_type="cpg",feature_id=features,feature_label=ifelse(is.na(genes)|!nzchar(genes),features,genes),
      contrast=colnames(model$contrasts)[k],tissue="whole blood",timepoint="18-month paired change difference",
      intervention="Diet-arm comparison conditional on five changes in closed estimated blood cell fractions",dose=NA_character_,duration="18 months",
      effect_estimate=fit_beta$coefficients[,k],standard_error=se,confidence_low=fit_beta$coefficients[,k]-critical*se,confidence_high=fit_beta$coefficients[,k]+critical*se,
      p_value=fit_m$p.value[,k],q_value=q[,k],q_value_contrast=p.adjust(fit_m$p.value[,k],"BH"),q_value_study_family=q[,k],
      effect_unit="delta delta beta",status="measured",n_people=n,n_samples=nrow(selected_info),contrast_assay_count=2*n,
      m_value_estimate=fit_m$coefficients[,k],m_value_standard_error=fit_m$stdev.unscaled[,k]*sqrt(fit_m$s2.post),
      chromosome=as.character(annotation$chr),position=as.character(annotation$pos),genome_build="hg19",annotation_gene=genes,
      cell_composition_adjusted=TRUE,analysis_role="sensitivity",stringsAsFactors=FALSE)
    write.table(results,con,sep="\t",quote=FALSE,row.names=FALSE,col.names=k==1L,na="NA");rm(results);gc(FALSE)
  },finally=close(con))
  # Source/environment drift cannot produce a normal completion manifest.
  for(record in c(list(identity$script,identity$helper,identity$parent_metadata,identity$parent_inputs,identity$matrix_manifest,identity$parent_annotation,
      identity$environment_lock,identity$source_listing,identity$raw_receipts,identity$executed_reference_function),identity$reference_files))
    if(dp_file_sha(record$path)!=record$sha256)dpb_error("Input/source/environment changed during sensitivity","input_hash_mismatch")
  metadata<-list(accession="E-MTAB-12527",analysis_version=paste0(parent$analysis_version,"-blood-composition-full-cohort-closed-v1"),
    parent_analysis_version=parent$analysis_version,analysis_role="sensitivity",run_utc=format(Sys.time(),tz="UTC",usetz=TRUE),
    script_path=normalizePath(script),script_sha256=dp_file_sha(script),helper_path=normalizePath(helper),helper_sha256=dp_file_sha(helper),
    environment_lock_path=lock,environment_lock_sha256=dp_file_sha(lock),identity_sha256=identity_sha,
    independent_people=length(people),sample_count=nrow(selected_info),source_people=256L,source_samples=512L,
    cell_composition_adjusted=TRUE,technical_batch_adjusted=FALSE,
    cell_estimation=list(method="minfi estimateCellCounts; sorted adult FlowSorted.Blood.450k projection",referencePlatform="IlluminaHumanMethylation450k",
      processMethod="auto resolves to preprocessQuantile for Blood",probeSelect="auto resolves to both for adult Blood",
      scope=identity$normalization_scope,EPIC_scope="EPIC arrays converted to common 450K probes; EPIC-only sites do not identify cell fractions",
      fitting_scope="All 512 deposited arrays jointly normalized with reference, including arrays later excluded from methylation model QC; model uses only parent included complete pairs",
      closure="Divide six nonnegative projected estimates by their sample sum before deriving five paired changes; Gran omitted",
      raw_sum_range=closure$raw_sum_range,roundoff_negative_count=closure$roundoff_negative_count,raw_minimum=closure$raw_minimum,
      estimation_uncertainty="Reference projection uncertainty is not propagated into model SE or CI"),
    design_formula="Individual 18-month minus baseline M/beta changes ~ 0 + diet arm + changes in closed CD8T,CD4T,NK,Bcell,Mono fractions; Gran reference",
    design_audit=model[setdiff(names(model),c("design","contrasts"))],
    beta_uncertainty="Separate beta-change limma fit/global eBayes gives moderated SE/95% CI; M-change model supplies P/FDR",
    multiple_testing="Separate sensitivity family: BH across every included CpG times three arm contrasts; per-contrast BH retained; not pooled with primary or other sensitivity runs",
    result_files=list("cpg_effects.tsv.gz"),auxiliary_files=c("estimated_cell_proportions.tsv","closed_cell_proportions.tsv","cell_design_audit.json","design_and_contrasts.rds","fitted_m_global_ebayes.rds","fitted_beta_global_ebayes.rds","resource_events.jsonl","cell_estimation_identity.json"),
    input_manifest=c(list(file_record(metadata_path),file_record(paired_path),file_record(tile_path),file_record(annotation_path),file_record(receipt_path),file_record(lock),file_record(counts_path)),identity$reference_files),
    output_manifest=lapply(file.path(output,c("cpg_effects.tsv.gz","fitted_m_global_ebayes.rds","fitted_beta_global_ebayes.rds","design_and_contrasts.rds")),file_record),
    package_versions=c(list(R=as.character(getRversion())),versions),review_status="pending",scientific_approval=FALSE,
    interpretation_limit="Conditional association sensitivity only; estimated composition can mediate diet response. No cell-intrinsic/direct-effect causal claim, personal health prediction, or automatic benefit. Primary total-response results remain separate; full-cohort quantile estimation is not equivalent to independently normalized chunks.")
  write_json(metadata,file.path(output,"analysis_metadata.json"),auto_unbox=TRUE,pretty=TRUE,na="null")
  list(status="completed_analysis_scientific_review_pending",independent_people=length(people),samples=nrow(selected_info),cell_estimation_source_samples=512L,contrasts=3L)
}

if(sys.nframe()==0L) {
  args<-commandArgs(trailingOnly=TRUE)
  script<-sub("^--file=","",grep("^--file=",commandArgs(),value=TRUE)[1L])
  output<-dpb_arg(args,"--output-dir","")
  if(!nzchar(output))stop("Provide a NEW --output-dir")
  dir.create(output,recursive=TRUE,showWarnings=FALSE)
  if(file.exists(file.path(output,"analysis_metadata.json")))stop("Completed output is immutable; use a new directory")
  if(!requireNamespace("jsonlite",quietly=TRUE))stop("Project jsonlite is required; no package installation performed")
  result<-tryCatch(dpb_main(args,script,output),error=function(error)list(
    status=if(inherits(error,"dpb_error"))error$code else "failed",
    reason=conditionMessage(error),primary_result_modified=FALSE,scientific_approval=FALSE))
  jsonlite::write_json(result,file.path(output,"sensitivity_status.json"),auto_unbox=TRUE,pretty=TRUE,na="null")
  cat(jsonlite::toJSON(result,auto_unbox=TRUE),"\n")
  if(result$status!="completed_analysis_scientific_review_pending")quit(status=2L)
}
