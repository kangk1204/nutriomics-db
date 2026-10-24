# Saving executed expression model state only; sourcing this file fits nothing.
expression_archive_sha256 <- function(path) {
  if(!file.exists(path))stop("Archive source missing: ",path)
  if(requireNamespace("openssl",quietly=TRUE)) {
    con<-file(path,"rb");on.exit(close(con));return(paste0(as.character(openssl::sha256(con)),collapse=""))
  }
  result<-system2("sha256sum",shQuote(path),stdout=TRUE,stderr=TRUE)
  if(!is.null(attr(result,"status"))||!grepl("^[a-f0-9]{64} ",result[1L]))stop("SHA256 unavailable")
  strsplit(result[1L]," +")[[1L]][1L]
}

expression_archive_contrast_axis_aliases <- function(design,contrasts,allow_intercept_alias=TRUE) {
  actual<-colnames(design);contrast_rows<-rownames(contrasts)
  if(is.null(actual)||is.null(contrast_rows)||length(actual)!=length(contrast_rows)||
     anyNA(actual)||anyNA(contrast_rows)||anyDuplicated(actual)||anyDuplicated(contrast_rows))
    stop("Model archive coefficient axes are unnamed, duplicated or incomplete")
  different<-which(actual!=contrast_rows)
  # makeContrasts renames only this intercept label. Never reorder or rename the saved axes.
  if(length(different)&&(!allow_intercept_alias||
     any(actual[different]!="(Intercept)"|contrast_rows[different]!="Intercept")))
    stop("Model archive coefficient axes differ beyond the limma intercept alias")
  lapply(different,function(index)list(index=as.integer(index),design_name=actual[index],
    contrast_row_name=contrast_rows[index],reason="limma makeContrasts intercept alias; original axes retained"))
}

save_expression_model_archive <- function(expression,sample_info,design,contrasts,
    unmoderated_fit,moderated_fit,path,voom_object=NULL,block=NULL,correlation=NULL,
    contrast_application="contrasts.fit",channel_direction=NULL,parent_provenance=NULL) {
  if(!is.matrix(expression)||!is.numeric(expression)||any(!is.finite(expression))||
     nrow(sample_info)!=ncol(expression)||nrow(design)!=ncol(expression)||
     nrow(contrasts)!=ncol(design)||
     !identical(rownames(expression),rownames(moderated_fit$coefficients)))stop("Model archive axes are inconsistent")
  if(!contrast_application%in%c("contrasts.fit","identity_untransformed"))stop("Unknown original contrast application")
  axis_aliases<-expression_archive_contrast_axis_aliases(design,contrasts,
    allow_intercept_alias=contrast_application=="contrasts.fit")
  sample_field<-intersect(c("sample_label","sample_accession","sample_id"),names(sample_info))
  if(length(sample_field)&&!identical(as.character(sample_info[[sample_field[1L]]]),colnames(expression)))
    stop("Sample metadata row order differs from actual expression columns")
  if(!is.null(voom_object)&&(!identical(voom_object$E,expression)||
     !identical(dim(voom_object$weights),dim(expression))||any(!is.finite(voom_object$weights))||any(voom_object$weights<=0)))
    stop("Actual voom expression and precision weights are required")
  if(!is.null(block)&&(length(block)!=ncol(expression)||anyNA(block)||
     length(correlation)!=1L||!is.finite(correlation)))stop("Actual block/correlation is required")
  if(contrast_application=="identity_untransformed"&&
     (!identical(dim(contrasts),c(ncol(design),ncol(design)))||any(contrasts!=diag(ncol(design)))))
    stop("Untransformed model requires exact identity contrast")
  if(file.exists(path))stop("Model archive already exists; completed archives are immutable")
  value<-list(schema_version="nutriomics-expression-model-archive-v3",archive_version="model-archive-20261022-v2",
    expression=expression,sample_info=sample_info,sample_ids=colnames(expression),design=design,contrasts=contrasts,
    contrast_design_axis_aliases=axis_aliases,
    voom_object=voom_object,voom_weights=if(is.null(voom_object))NULL else voom_object$weights,
    block=block,correlation=correlation,unmoderated_fit=unmoderated_fit,moderated_contrast_fit=moderated_fit,
    contrast_application=contrast_application,channel_direction=channel_direction,parent_provenance=parent_provenance,
    moderation_scope="One whole retained-feature fit; original contrast family unchanged",
    limma_version=as.character(packageVersion("limma")),scientific_approval=FALSE,expert_review_status="pending")
  temporary<-paste0(path,".writing");saveRDS(value,temporary,compress=FALSE)
  if(!file.rename(temporary,path))stop("Model archive atomic install failed")
  list(file=basename(path),sha256=expression_archive_sha256(path),bytes=file.info(path)$size,
    schema_version=value$schema_version,archive_version=value$archive_version,
    expression_dimensions=dim(expression),design_dimensions=dim(design),contrast_dimensions=dim(contrasts),
    contrast_design_axis_aliases=axis_aliases,
    actual_voom_weights_saved=!is.null(voom_object),actual_block_saved=!is.null(block),
    original_model_statistics_recomputed_by_archiving=FALSE,scientific_approval=FALSE,expert_review_status="pending")
}

refit_expression_model_archive <- function(archive) {
  if(!identical(archive$schema_version,"nutriomics-expression-model-archive-v3"))stop("Unknown actual model archive schema")
  aliases<-expression_archive_contrast_axis_aliases(archive$design,archive$contrasts,
    allow_intercept_alias=archive$contrast_application=="contrasts.fit")
  if(!is.null(archive$contrast_design_axis_aliases)&&!identical(aliases,archive$contrast_design_axis_aliases))
    stop("Saved coefficient-axis alias receipt disagrees with actual matrices")
  input<-if(is.null(archive$voom_object))archive$expression else archive$voom_object
  if(!is.null(archive$voom_object)&&!identical(archive$voom_weights,archive$voom_object$weights))stop("Saved voom weights disagree")
  arguments<-list(object=input,design=archive$design)
  if(!is.null(archive$block)) {
    arguments$block<-archive$block;arguments$correlation<-archive$correlation
  }
  unmoderated<-do.call(limma::lmFit,arguments)
  transformed<-if(archive$contrast_application=="identity_untransformed")unmoderated else limma::contrasts.fit(unmoderated,archive$contrasts)
  list(unmoderated_fit=unmoderated,moderated_contrast_fit=limma::eBayes(transformed,robust=TRUE))
}
