# ballot management (voting, commenting, writeups, ...) for Area
# Directors and Secretariat

import re, os
from datetime import datetime, date, time, timedelta
from django.http import HttpResponse, HttpResponseRedirect, Http404
from django.shortcuts import render_to_response, get_object_or_404, redirect
from django.core.urlresolvers import reverse as urlreverse
from django.template.loader import render_to_string
from django.template import RequestContext
from django import forms
from django.utils.html import strip_tags
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from ietf.utils.mail import send_mail_text, send_mail_preformatted
from ietf.ietfauth.decorators import group_required
from ietf.idtracker.templatetags.ietf_filters import in_group
from ietf.ietfauth.decorators import has_role
from mails import email_secretariat

from utils import *
from group.models import Group, GroupHistory, GroupEvent, save_group_in_history
from name.models import GroupBallotPositionName, CharterDocStateName, GroupStateName
from doc.models import Document, DocEvent, GroupBallotPositionDocEvent, WriteupDocEvent

def default_action_text(wg, doc, user):
   e = WriteupDocEvent(doc=doc, by=user)
   e.by = user
   e.type = "changed_action_announcement"
   e.desc = "WG action text was changed"
   e.text = "The %s (%s) working group " % (wg.name, wg.acronym)
   if wg.parent:
       e.text += "in the %s " % wg.parent.name
   e.text += "of the IETF has been "
   if wg.state_id == "proposed":
       e.text += "proposed"
   else:
       e.text += " rechartered"
   e.text += ". \nFor additional information, please contact the Area Directors or the working group Chairs."
   e.save()
   return e

def default_review_text(wg, doc, user):
   e = WriteupDocEvent(doc=doc, by=user)
   e.by = user
   e.type = "changed_review_announcement"
   e.desc = "WG review text was changed"
   if wg.state_id == "proposed":
       e.text = "A charter"
   else:
       e.text = "A modified charter"
   e.text += " has been submitted for the %s (%s) working group \n" % (wg.name, wg.acronym)
   if wg.parent:
       e.text += "in the %s " % wg.parent.name
   e.text += "of the IETF. The charter is provided below for\n"
   e.text += "informational purposes only. Please send your comments to the\n"
   e.text += "IESG mailing list (iesg at ietf.org) within one week from today."
   e.save()
   return e

BALLOT_CHOICES = (("yes", "Yes"),
                  ("no", "No"),
                  ("block", "Block"),
                  ("abstain", "Abstain"),
                  ("", "No Record"),
                  )

def position_to_ballot_choice(position):
    for v, label in BALLOT_CHOICES:
        if v and getattr(position, v):
            return v
    return ""

def position_label(position_value):
    return dict(BALLOT_CHOICES).get(position_value, "")

class EditPositionForm(forms.Form):
    position = forms.ModelChoiceField(queryset=GroupBallotPositionName.objects.all(), widget=forms.RadioSelect, initial="norecord", required=True)
    block_comment = forms.CharField(required=False, label="Blocking comment", widget=forms.Textarea)
    comment = forms.CharField(required=False, widget=forms.Textarea)
    return_to_url = forms.CharField(required=False, widget=forms.HiddenInput)

    def clean_blocking(self):
       entered_blocking = self.cleaned_data["block_comment"]
       entered_pos = self.cleaned_data["position"]
       if entered_pos.slug == "block" and not entered_blocking:
           raise forms.ValidationError("You must enter a non-empty blocking comment")
       return entered_blocking

@group_required('Area_Director','Secretariat')
def edit_position(request, name):
    """Vote and edit comments on Charter as Area Director."""
    try:
        wg = Group.objects.get(acronym=name)
    except ObjectDoesNotExist:
        wglist = GroupHistory.objects.filter(acronym=name)
        if wglist:
            return redirect('wg_edit_position', name=wglist[0].group.acronym)
        else:
            raise Http404

    doc = set_or_create_charter(wg)
    started_process = doc.latest_event(type="started_iesg_process")

    ad = login = request.user.get_profile()

    if 'HTTP_REFERER' in request.META:
        return_to_url = request.META['HTTP_REFERER']
    else:
        return_to_url = doc.get_absolute_url()

    # if we're in the Secretariat, we can select an AD to act as stand-in for
    if not has_role(request.user, "Area Director"):
        ad_id = request.GET.get('ad')
        if not ad_id:
            raise Http404()
        from person.models import Person
        ad = get_object_or_404(Person, pk=ad_id)

    old_pos = doc.latest_event(GroupBallotPositionDocEvent, type="changed_ballot_position", ad=ad, time__gte=started_process.time)

    if request.method == 'POST':
        form = EditPositionForm(request.POST)
        if form.is_valid():
 
            # save the vote
            clean = form.cleaned_data

            if clean['return_to_url']:
              return_to_url = clean['return_to_url']

            pos = GroupBallotPositionDocEvent(doc=doc, by=login)
            pos.type = "changed_ballot_position"
            pos.ad = ad
            pos.pos = clean["position"]
            pos.comment = clean["comment"].strip()
            pos.comment_time = old_pos.comment_time if old_pos else None
            pos.block_comment = clean["block_comment"].strip() if pos.pos_id == "block" else ""
            pos.block_comment_time = old_pos.block_comment_time if old_pos else None

            changes = []
            added_events = []
            # possibly add discuss/comment comments to history trail
            # so it's easy to see
            old_comment = old_pos.comment if old_pos else ""
            if pos.comment != old_comment:
                pos.comment_time = pos.time
                changes.append("comment")

                if pos.comment:
                    e = DocEvent(doc=doc)
                    e.by = ad # otherwise we can't see who's saying it
                    e.type = "added_comment"
                    e.desc = "[Ballot comment]\n" + pos.comment
                    added_events.append(e)

            old_block_comment = old_pos.block_comment if old_pos else ""
            if pos.block_comment != old_block_comment:
                pos.block_comment_time = pos.time
                changes.append("block_comment")

                if pos.block_comment:
                    e = DocEvent(doc=doc, by=login)
                    e.by = ad # otherwise we can't see who's saying it
                    e.type = "added_comment"
                    e.desc = "[Ballot blocking comment]\n" + pos.block_comment
                    added_events.append(e)

            # figure out a description
            if not old_pos and pos.pos.slug != "norecord":
                pos.desc = u"[Ballot Position Update] New position, %s, has been recorded for %s" % (pos.pos.name, pos.ad.name)
            elif old_pos and pos.pos != old_pos.pos:
                pos.desc = "[Ballot Position Update] Position for %s has been changed to %s from %s" % (pos.ad.name, pos.pos.name, old_pos.pos.name)

            if not pos.desc and changes:
                pos.desc = u"Ballot %s text updated for %s" % (u" and ".join(changes), ad.name)

            # only add new event if we actually got a change
            if pos.desc:
                if login != ad:
                    pos.desc += u" by %s" % login.name

                pos.save()

                for e in added_events:
                    e.save() # save them after the position is saved to get later id
                        
                doc.time = pos.time
                doc.save()

            if request.POST.get("send_mail"):
                qstr = "?return_to_url=%s" % return_to_url
                if request.GET.get('ad'):
                    qstr += "&ad=%s" % request.GET.get('ad')
                return HttpResponseRedirect(urlreverse("wg_send_ballot_comment", kwargs=dict(name=wg.acronym)) + qstr)
            else:
                return HttpResponseRedirect(return_to_url)
    else:
        initial = {}
        if old_pos:
            initial['position'] = old_pos.pos.slug
            initial['block_comment'] = old_pos.block_comment
            initial['comment'] = old_pos.comment
            
        if return_to_url:
            initial['return_to_url'] = return_to_url
            
        form = EditPositionForm(initial=initial)

    return render_to_response('wgrecord/edit_position.html',
                              dict(doc=doc,
                                   wg=wg,
                                   form=form,
                                   ad=ad,
                                   return_to_url=return_to_url,
                                   old_pos=old_pos,
                                   ),
                              context_instance=RequestContext(request))

@group_required('Area_Director','Secretariat')
def send_ballot_comment(request, name):
    """Email Charter ballot comment for area director."""
    try:
        wg = Group.objects.get(acronym=name)
    except ObjectDoesNotExist:
        wglist = GroupHistory.objects.filter(acronym=name)
        if wglist:
            return redirect('wg_send_ballot_comment', name=wglist[0].group.acronym)
        else:
            raise Http404

    doc = set_or_create_charter(wg)
    started_process = doc.latest_event(type="started_iesg_process")
    if not started_process:
        raise Http404()

    ad = login = request.user.get_profile()

    return_to_url = request.GET.get('return_to_url')
    if not return_to_url:
        return_to_url = doc.get_absolute_url()

    if 'HTTP_REFERER' in request.META:
        back_url = request.META['HTTP_REFERER']
    else:
        back_url = doc.get_absolute_url()

    # if we're in the Secretariat, we can select an AD to act as stand-in for
    if not has_role(request.user, "Area Director"):
        ad_id = request.GET.get('ad')
        if not ad_id:
            raise Http404()
        from person.models import Person
        ad = get_object_or_404(Person, pk=ad_id)

    pos = doc.latest_event(GroupBallotPositionDocEvent, type="changed_ballot_position", ad=ad, time__gte=started_process.time)
    if not pos:
        raise Http404()
    
    subj = []
    d = ""
    if pos.pos_id == "block" and pos.block_comment:
        d = pos.block_comment
        subj.append("BLOCKING COMMENT")
    c = ""
    if pos.comment:
        c = pos.comment
        subj.append("COMMENT")

    ad_name_genitive = ad.name + "'" if ad.name.endswith('s') else ad.name + "'s"
    subject = "%s %s on %s" % (ad_name_genitive, pos.pos.name if pos.pos else "No Position", doc.name + "-" + doc.rev)
    if subj:
        subject += ": (with %s)" % " and ".join(subj)

    doc.filename = doc.name # compatibility attributes
    doc.revision_display = doc.rev
    body = render_to_string("wgrecord/ballot_comment_mail.txt",
                            dict(block_comment=d, comment=c, ad=ad.name, doc=doc, pos=pos.pos))
    frm = ad.formatted_email()
    to = "The IESG <iesg@ietf.org>"
        
    if request.method == 'POST':
        cc = [x.strip() for x in request.POST.get("cc", "").split(',') if x.strip()]
        if request.POST.get("cc_state_change") and doc.notify:
            cc.extend(doc.notify.split(','))

        send_mail_text(request, to, frm, subject, body, cc=", ".join(cc))
            
        return HttpResponseRedirect(return_to_url)
  
    return render_to_response('wgrecord/send_ballot_comment.html',
                              dict(doc=doc,
                                   subject=subject,
                                   body=body,
                                   frm=frm,
                                   to=to,
                                   ad=ad,
                                   can_send=d or c,
                                   back_url=back_url,
                                  ),
                              context_instance=RequestContext(request))
        
class AnnouncementTextForm(forms.Form):
    announcement_text = forms.CharField(widget=forms.Textarea, required=True)

    def clean_announcement_text(self):
        return self.cleaned_data["announcement_text"].replace("\r", "")

@group_required('Area_Director','Secretariat')
def announcement_text(request, name, ann):
    """Editing of announcement text"""
    try:
        wg = Group.objects.get(acronym=name)
    except ObjectDoesNotExist:
        wglist = GroupHistory.objects.filter(acronym=name)
        if wglist:
            return redirect('wg_announcement_text', name=wglist[0].group.acronym)
        else:
            raise Http404

    doc = set_or_create_charter(wg)

    login = request.user.get_profile()

    if ann == "action":
        existing = doc.latest_event(WriteupDocEvent, type="changed_action_announcement")
    elif ann == "review":
        existing = doc.latest_event(WriteupDocEvent, type="changed_review_announcement")
    if not existing:
        if ann == "action":
            existing = default_action_text(wg, doc, login)
        elif ann == "review":
            existing = default_review_text(wg, doc, login)

    form = AnnouncementTextForm(initial=dict(announcement_text=existing.text))

    if request.method == 'POST':
        form = AnnouncementTextForm(request.POST)
        if form.is_valid():
            t = form.cleaned_data['announcement_text']
            if t != existing.text:
                e = WriteupDocEvent(doc=doc, by=login)
                e.by = login
                e.type = "changed_%s_announcement" % ann
                e.desc = "WG %s text was changed" % ann
                e.text = t
                e.save()
                
                doc.time = e.time
                doc.save()
        return redirect('wg_view_record', name=wg.acronym)
    return render_to_response('wgrecord/announcement_text.html',
                              dict(doc=doc,
                                   announcement=ann,
                                   back_url=doc.get_absolute_url(),
                                   announcement_text_form=form,
                                   ),
                              context_instance=RequestContext(request))

@group_required('Secretariat')
def approve_ballot(request, name):
    """Approve ballot, changing state, copying charter"""
    try:
        wg = Group.objects.get(acronym=name)
    except ObjectDoesNotExist:
        wglist = GroupHistory.objects.filter(acronym=name)
        if wglist:
            return redirect('wg_approve_ballot', name=wglist[0].group.acronym)
        else:
            raise Http404

    doc = set_or_create_charter(wg)

    login = request.user.get_profile()

    e = doc.latest_event(WriteupDocEvent, type="changed_action_announcement")
    if not e:
        announcement = default_action_text(wg, doc, login)
    else:
        announcement = e.text

    if request.method == 'POST':
        new_state = GroupStateName.objects.get(slug="active")
        new_charter_state = CharterDocStateName.objects.get(slug="approved")

        save_charter_in_history(doc)
        save_group_in_history(wg)

        prev_state = wg.state
        prev_charter_state = doc.charter_state
        wg.state = new_state
        doc.charter_state = new_charter_state

        e = DocEvent(doc=doc, by=login)
        e.type = "iesg_approved"
        e.desc = "IESG has approved the charter"

        e.save()
        
        change_description = e.desc + " and WG state has been changed to %s" % new_state.name
        
        e = log_state_changed(request, doc, login, prev_state)
                    
        wg.time = e.time
        wg.save()

        filename = os.path.join(doc.get_file_path(), doc.name+"-"+doc.rev+".txt")
        try:
           source = open(filename, 'rb')
           raw_content = source.read()

           doc.rev = next_approved_revision(doc.rev)
           
           new_filename = os.path.join(doc.get_file_path(), doc.name+"-"+doc.rev+".txt")
           destination = open(new_filename, 'wb+')
           destination.write(raw_content)
           destination.close()
        except IOError:
           raise Http404

        doc.save()
        
        email_secretariat(request, wg, "state-%s" % doc.charter_state_id, change_description)

        # send announcement
        send_mail_preformatted(request, announcement)

        return HttpResponseRedirect(doc.get_absolute_url())
  
    return render_to_response('wgrecord/approve_ballot.html',
                              dict(doc=doc,
                                   announcement=announcement,
                                   wg=wg),
                              context_instance=RequestContext(request))

