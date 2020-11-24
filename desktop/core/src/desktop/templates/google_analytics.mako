<%!

  from desktop import conf
%>

% if conf.GOOGLE_GTAG.get():
<!-- Global site tag (gtag.js) - Google Analytics (a.mazurov@criteo.com) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=${ conf.GOOGLE_GTAG.get() }"></script>
<script>
window.dataLayer = window.dataLayer || [];
function gtag(){dataLayer.push(arguments);}
gtag('js', new Date());

gtag('config', '${ conf.GOOGLE_GTAG.get() }');
</script>
% endif
