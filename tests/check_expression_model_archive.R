# Synthetic state roundtrip, including the actual correlated weighted fit.
args<-commandArgs(trailingOnly=TRUE)
source(if(length(args))args[1L] else "scripts/expression_archive_helpers.R")
suppressPackageStartupMessages(library(limma))
assert_error<-function(expr)stopifnot(inherits(tryCatch({force(expr);NULL},error=identity),"error"))
set.seed(787113)
folder<-tempfile("expression-archive-");dir.create(folder)
samples<-paste0("S",1:36);features<-paste0("gene",1:180)
info<-data.frame(sample_id=samples,subject_id=factor(rep(1:12,3)),visit=factor(rep(1:3,each=12)))
E<-matrix(rnorm(180*36),180,dimnames=list(features,samples))
weights<-matrix(exp(rnorm(180*36)),180,dimnames=dimnames(E))
v<-new("EList",list(E=E,weights=weights,targets=data.frame(lib.size=rep(1e6,36))))
design<-model.matrix(~visit,info)
C<-makeContrasts(visit2=visit2,visit3=visit3,levels=design)
# The real GSE127530 failure: limma preserves the coefficient positions but labels
# the contrast intercept "Intercept" while model.matrix labels it "(Intercept)".
stopifnot(identical(colnames(design),c("(Intercept)","visit2","visit3")),
 identical(rownames(C),c("Intercept","visit2","visit3")))
unmoderated<-lmFit(v,design,block=info$subject_id,correlation=.25)
moderated<-eBayes(contrasts.fit(unmoderated,C),robust=TRUE)
state<-.Random.seed
record<-save_expression_model_archive(E,info,design,C,unmoderated,moderated,file.path(folder,"weighted.rds"),
 voom_object=v,block=info$subject_id,correlation=.25)
stopifnot(identical(state,.Random.seed),record$actual_voom_weights_saved,record$actual_block_saved,!record$scientific_approval)
archive<-readRDS(file.path(folder,"weighted.rds"));replay<-refit_expression_model_archive(archive)
stopifnot(identical(archive$voom_weights,weights),identical(archive$block,info$subject_id),archive$correlation==.25)
stopifnot(identical(archive$design,design),identical(archive$contrasts,C),
 identical(archive$unmoderated_fit,unmoderated),identical(archive$moderated_contrast_fit,moderated),
 identical(archive$archive_version,"model-archive-20261022-v2"),
 identical(archive$contrast_design_axis_aliases,record$contrast_design_axis_aliases),
 length(archive$contrast_design_axis_aliases)==1L,
 archive$contrast_design_axis_aliases[[1L]]$index==1L,
 archive$contrast_design_axis_aliases[[1L]]$design_name=="(Intercept)",
 archive$contrast_design_axis_aliases[[1L]]$contrast_row_name=="Intercept")
for(key in c("coefficients","p.value","s2.post","stdev.unscaled","df.total","t"))
 stopifnot(isTRUE(all.equal(replay$moderated_contrast_fit[[key]],moderated[[key]],tolerance=1e-12)))
wrong<-eBayes(contrasts.fit(lmFit(E,design),C),robust=TRUE)
stopifnot(!isTRUE(all.equal(wrong$coefficients,moderated$coefficients)))
assert_error(save_expression_model_archive(E,info,design,C,unmoderated,moderated,file.path(folder,"weighted.rds"),voom_object=v,block=info$subject_id,correlation=.25))
v_bad<-v;v_bad$weights[1,1]<-0
assert_error(save_expression_model_archive(E,info,design,C,unmoderated,moderated,file.path(folder,"bad.rds"),voom_object=v_bad))
for(kind in c("wrong_intercept","reordered_slopes","unrelated_alias","unnamed","duplicate")) {
 C_bad<-C
 if(kind=="wrong_intercept")rownames(C_bad)[1L]<-"NotIntercept"
 if(kind=="reordered_slopes")C_bad<-C_bad[c(1L,3L,2L),,drop=FALSE]
 if(kind=="unrelated_alias")rownames(C_bad)[2L]<-"visit_2"
 if(kind=="unnamed")rownames(C_bad)<-NULL
 if(kind=="duplicate")rownames(C_bad)[3L]<-rownames(C_bad)[2L]
 assert_error(save_expression_model_archive(E,info,design,C_bad,unmoderated,moderated,
   file.path(folder,paste0(kind,".rds")),voom_object=v,block=info$subject_id,correlation=.25))
 stopifnot(!file.exists(file.path(folder,paste0(kind,".rds"))))
}
changed_receipt<-archive;changed_receipt$contrast_design_axis_aliases[[1L]]$index<-2L
assert_error(refit_expression_model_archive(changed_receipt))
# Seven arrays are seven paired log-ratios, not fourteen independent people.
ratio<-matrix(rnorm(180*7),180,dimnames=list(features,paste0("P",1:7)))
people<-data.frame(sample_accession=colnames(ratio),subject_id=1:7)
intercept<-matrix(1,7,1,dimnames=list(colnames(ratio),"Cy5_8w_vs_Cy3_baseline"))
identity_contrast<-matrix(1,1,1,dimnames=list(colnames(intercept),colnames(intercept)))
fit0<-lmFit(ratio,intercept);fit1<-eBayes(fit0,robust=TRUE)
save_expression_model_archive(ratio,people,intercept,identity_contrast,fit0,fit1,file.path(folder,"two-colour.rds"),
 contrast_application="identity_untransformed",channel_direction=list(Cy3="baseline",Cy5="8 weeks"))
two<-readRDS(file.path(folder,"two-colour.rds"));again<-refit_expression_model_archive(two)
stopifnot(is.null(two$voom_object),is.null(two$voom_weights),is.null(two$block),nrow(two$sample_info)==7L,
 length(two$contrast_design_axis_aliases)==0L,
 identical(two$channel_direction,list(Cy3="baseline",Cy5="8 weeks")))
for(key in c("coefficients","p.value","s2.post","stdev.unscaled","df.total","t"))
 stopifnot(isTRUE(all.equal(again$moderated_contrast_fit[[key]],fit1[[key]],tolerance=1e-12)))
stopifnot(!file.exists(file.path(folder,"bad.rds")))
unlink(folder,recursive=TRUE)
cat("Correlated weighted limma intercept alias and two-colour identity archive replay passed; original fit numbers/RNG preserved\n")
