from treenipaivakirja.models import harjoitus, aika, laji, teho, tehoalue, kausi
from treenipaivakirja.forms import HarjoitusForm, LajiForm, TehoForm, TehoalueForm, UserForm, RegistrationForm, KausiForm
import treenipaivakirja.transformations as transformations
from django.db.models import Sum, Max, Min
from django.shortcuts import render,redirect
from django.forms import inlineformset_factory, modelformset_factory, formset_factory
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.models import User
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.decorators import login_required
from django.db.models.deletion import ProtectedError
from django.db import IntegrityError
from django.http import JsonResponse, HttpResponse
from django.conf import settings

from datetime import datetime, timedelta
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows
import pandas as pd
import numpy as np
import os
import json
import logging

LOGGER_DEBUG = logging.getLogger(__name__)


@login_required
def index(request):
    """ 
    Front page 
    """
    current_user_id = request.user.id
    current_day = datetime.now().date()
    current_year = current_day.year
    current_week = current_day.strftime("%V")
    current_day_pd = pd.Timestamp(current_day)
    current_day_minus_14 = pd.Timestamp(current_day_pd - timedelta(days=14))
    current_day_minus_28 = pd.Timestamp(current_day_pd - timedelta(days=28))

    if not harjoitus.objects.filter(user=current_user_id):
        hours_current_year = 0
        hours_change = 0
        hours_per_week_current_year = 0
        hours_per_week_change = 0
        feeling_current_period = 0
        feeling_change = 0
    else:
        trainings = harjoitus.objects.filter(user=current_user_id).values_list('aika__pvm','aika__vuosi','aika__vko','kesto','tuntuma')
        trainings_df = pd.DataFrame(list(trainings), columns=['Päivä','Vuosi','Viikko','Kesto','Tuntuma'])
        trainings_df = trainings_df.fillna(np.nan)  #replace None with NaN
        trainings_df['Päivä'] = pd.to_datetime(trainings_df['Päivä'])
        trainings_df['Kesto'] = trainings_df['Kesto'].fillna(0).astype(float).round(1)

        hours_current_year = trainings_df[(trainings_df['Vuosi'] == current_year) & (trainings_df['Päivä'] <= current_day_pd)]['Kesto'].sum()
        hours_past_year = trainings_df[(trainings_df['Vuosi'] == (current_year-1)) & (trainings_df['Päivä'] <= pd.Timestamp(current_day_pd - timedelta(days=365)))]['Kesto'].sum()
        hours_change = hours_current_year - hours_past_year

        hours_per_week_current_year = hours_current_year / (int(current_week)-1)
        hours_per_week_past_year = trainings_df[trainings_df['Vuosi'] == (current_year-1)]['Kesto'].sum() / 52
        hours_per_week_change = hours_per_week_current_year - hours_per_week_past_year

        feeling_current_period = transformations.coalesce(trainings_df[(trainings_df['Päivä'] >= current_day_minus_14) & (trainings_df['Päivä'] <= current_day_pd)]['Tuntuma'].mean(),0)
        feeling_last_period = transformations.coalesce(trainings_df[(trainings_df['Päivä'] >= current_day_minus_28) & (trainings_df['Päivä'] <= current_day_minus_14)]['Tuntuma'].mean(),0)
        feeling_change = feeling_current_period - feeling_last_period

    return render(request,'index.html',
        context = {
            'hours_current_year': int(round(hours_current_year,0)),
            'hours_change': int(round(hours_change,0)),
            'hours_per_week_current_year': round(hours_per_week_current_year,1),
            'hours_per_week_change': round(hours_per_week_change,1),
            'feeling_current_period': round(feeling_current_period,1),
            'feeling_change': round(feeling_change,1),
            'current_day': current_day
            })


@login_required
def trainings_view(request):
    """ 
    List of trainings 
    """
    current_user_id = request.user.id
    current_day = datetime.now().date()
    first_day = harjoitus.objects.filter(user=current_user_id).aggregate(Min('aika__pvm'))['aika__pvm__min']
    startdate = transformations.coalesce(first_day,current_day).strftime('%d.%m.%Y')
    enddate = current_day.strftime('%d.%m.%Y') 
    sports = transformations.sports_dict(current_user_id)
    sport = 'Kaikki'

    zones = list(teho.objects.filter(harjoitus_id__user=current_user_id).values_list('tehoalue_id__tehoalue',flat=True).distinct().order_by('tehoalue_id__jarj_nro'))
    table_headers = ['edit','delete','Vko','Päivä','Laji','Kesto','Keskisyke','Matka (km)','Vauhti (km/h)','Tuntuma','Kommentti']
    table_headers = table_headers[:-1] + zones + table_headers[-1:]
    
    if request.method == "POST":
        sport = request.POST['sport']
        startdate = request.POST['startdate']
        enddate = request.POST['enddate']
        startdate_dt = datetime.strptime(startdate,'%d.%m.%Y')
        startdate_yyyymmdd = startdate_dt.strftime('%Y%m%d')
        enddate_dt = datetime.strptime(enddate,'%d.%m.%Y')
        enddate_yyyymmdd = enddate_dt.strftime('%Y%m%d')

        trainings_df = transformations.trainings_datatable(current_user_id)

        if trainings_df is None:
            messages.add_message(request, messages.ERROR, 'Ei harjoituksia')
        else:
            trainings_df = trainings_df[(trainings_df['vvvvkkpp']>=int(startdate_yyyymmdd)) & (trainings_df['vvvvkkpp']<=int(enddate_yyyymmdd))]    
            if sport != 'Kaikki':
                if sport in sports.keys():
                    trainings_df = trainings_df[trainings_df['Lajiryhmä'] == sport]
                else:
                    trainings_df = trainings_df[trainings_df['Laji'] == sport]
            if trainings_df.empty:
                messages.add_message(request, messages.ERROR, 'Ei harjoituksia')
            else:
                export_columns = ['Vko','Pvm','Viikonpäivä','Kesto (h)','Laji','Matka (km)','Vauhti (km/h)','Vauhti (min/km)','Keskisyke', 'Nousu (m)','Tuntuma', 'Kommentti'] + zones
                trainings_export = trainings_df[export_columns]
                trainings_export = trainings_export.sort_values(by='Pvm', ascending=True)
                trainings_export['Pvm'] = pd.to_datetime(trainings_df['Pvm']).dt.strftime('%d.%m.%Y')

                if 'export_csv' in request.POST:
                    try:
                        response = HttpResponse(content_type='text/csv')
                        response['Content-Disposition'] = 'attachment; filename="treenit.csv"'
                        trainings_export.to_csv(response,sep=';',header=True,index=False,encoding='utf-8')
                        return response
                    except Exception as e:
                        messages.add_message(request, messages.ERROR, 'Lataus epäonnistui: {}'.format(str(e)))

                if 'export_xls' in request.POST:
                    try:
                        response = HttpResponse(content_type='application/ms-excel')
                        response['Content-Disposition'] = 'attachment; filename="treenit.xlsx"'
                        wb = Workbook()
                        ws = wb.active
                        for r in dataframe_to_rows(trainings_export, index=False, header=True):
                            ws.append(r)
                        wb.save(response)
                        return response
                    except Exception as e:
                        messages.add_message(request, messages.ERROR, 'Lataus epäonnistui: {}'.format(str(e)))

    return render(request, 'trainings.html',
        context = {
            'sport': sport,
            'sports': sports,
            'startdate': startdate,
            'enddate': enddate,
            'table_headers': table_headers
            })


@login_required
def reports(request):
    """ 
    Trainings reports 
    """
    current_user_id = request.user.id
    current_year = str(datetime.now().year)

    if not harjoitus.objects.filter(user=current_user_id):
        years = [current_year]
        sport = ''
        sports = []
        hours_per_year_json = []
        kilometers_per_year_json = []
        hours_per_month_json = []
        hours_per_week_json = []
        hours_per_sport_json = []
        hours_per_sport_group_json = []
        hours_per_year_per_sport_json = []
        kilometers_per_year_per_sport_json = []
        avg_per_year_per_sport = []
        amounts_per_year_per_sport = []
        avg_per_year_per_sport_table = []
        hours_per_year_per_zone_json = []
    else:
        trainings_df = transformations.trainings(current_user_id)
        sports = transformations.sports_list(current_user_id) 
        sport = sports[0]
        years = trainings_df.sort_values('vuosi')['vuosi'].unique()

        trainings_per_year = transformations.trainings_per_year(trainings_df)
        trainings_per_month = transformations.trainings_per_month(trainings_df,current_user_id)
        trainings_per_week = transformations.trainings_per_week(trainings_df,current_user_id)
        trainings_per_sport = transformations.trainings_per_sport(trainings_df)

        hours_per_year_json = transformations.hours_per_year(trainings_per_year)
        hours_per_month_json = transformations.hours_per_month(trainings_per_month)
        hours_per_week_json = transformations.hours_per_week(trainings_per_week)
        hours_per_sport_json = transformations.hours_per_sport(trainings_df)
        hours_per_sport_group_json = transformations.hours_per_sport_group(trainings_df)
        kilometers_per_year_json = transformations.kilometers_per_year(trainings_per_year)
        hours_per_year_per_zone_json = transformations.hours_per_year_per_zone(trainings_df,current_user_id)
        
        hours_per_year_per_sport = {}
        kilometers_per_year_per_sport = {}
        avg_per_year_per_sport = {}
        avg_per_year_per_sport_table = {}
        amounts_per_year_per_sport = {}

        for s in sports:
            data = trainings_per_sport[trainings_per_sport['laji_nimi'] == s]
            if not data.empty:
                amounts_per_year_per_sport[s] = data[['vuosi','lkm','kesto (h)','matka (km)']].fillna('').to_dict(orient='records')
                avg_per_year_per_sport_table[s] = data[['vuosi','kesto (h) ka.','matka (km) ka.','vauhti (km/h)','keskisyke']].rename(columns={'kesto (h) ka.':'kesto (h)','matka (km) ka.':'matka (km)'}).fillna('').to_dict(orient='records')
                data = data.set_index('vuosi')
                hours_per_year_per_sport[s] = json.loads(transformations.dataframe_to_json(data[['kesto (h)']]))
                kilometers_per_year_per_sport[s] = json.loads(transformations.dataframe_to_json(data[['matka (km)']]))
                avg_per_year_per_sport[s] = json.loads(transformations.dataframe_to_json(data[['vauhti (km/h)','keskisyke']]))
        
        hours_per_year_per_sport_json = json.dumps(hours_per_year_per_sport)
        kilometers_per_year_per_sport_json = json.dumps(kilometers_per_year_per_sport)

    return render(request, 'reports.html',
        context = {
            'current_year': current_year,
            'years': years,
            'sport': sport,
            'sports': sports,
            'hours_per_year_json': hours_per_year_json,
            'kilometers_per_year_json': kilometers_per_year_json,
            'hours_per_month_json': hours_per_month_json,
            'hours_per_week_json': hours_per_week_json,
            'hours_per_sport_json': hours_per_sport_json,
            'hours_per_sport_group_json': hours_per_sport_group_json,
            'hours_per_year_per_sport_json': hours_per_year_per_sport_json,
            'kilometers_per_year_per_sport_json': kilometers_per_year_per_sport_json,
            'avg_per_year_per_sport': avg_per_year_per_sport,
            'amounts_per_year_per_sport': amounts_per_year_per_sport,
            'avg_per_year_per_sport_table': avg_per_year_per_sport_table,
            'hours_per_year_per_zone_json': hours_per_year_per_zone_json
            })


@login_required
def training_add(request):
    """ 
    Inserts new training 
    """
    max_count = 20
    TehoFormset = inlineformset_factory(harjoitus,teho,form=TehoForm,extra=max_count,max_num=max_count,can_delete=True)
    if request.method == "POST":
        harjoitus_form = HarjoitusForm(request.user,request.POST)
        teho_formset = TehoFormset(request.POST)
        if harjoitus_form.is_valid() and harjoitus_form.has_changed():
            instance = harjoitus_form.save(commit=False)
            instance.aika_id = instance.pvm.strftime('%Y%m%d')
            instance.user = request.user
            kesto_h = transformations.coalesce(harjoitus_form.cleaned_data['kesto_h'],0)
            kesto_min = transformations.coalesce(harjoitus_form.cleaned_data['kesto_min'],0)
            instance.kesto_h = kesto_h
            instance.kesto_min = kesto_min
            instance.kesto = transformations.h_min_to_hours(kesto_h,kesto_min)
            vauhti_m = harjoitus_form.cleaned_data['vauhti_min']
            vauhti_s = harjoitus_form.cleaned_data['vauhti_s']
            instance.vauhti_min_km = transformations.vauhti_min_km(vauhti_m,vauhti_s)
            vauhti_km_h = harjoitus_form.cleaned_data['vauhti_km_h']
            if instance.vauhti_min_km is None and vauhti_km_h is not None:
                instance.vauhti_min_km = 60 / vauhti_km_h
            elif instance.vauhti_min_km is not None and vauhti_km_h is None:
                instance.vauhti_km_h = 60 / instance.vauhti_min_km
            instance.save()
            training = harjoitus.objects.get(id=instance.id)
            teho_formset = TehoFormset(request.POST, request.FILES,instance=training)
            if teho_formset.is_valid() and teho_formset.has_changed():
                teho_formset.save()
            return redirect('trainings')
    else:
        harjoitus_form = HarjoitusForm(request.user,initial={'pvm': datetime.now()})
        teho_formset = TehoFormset(queryset=teho.objects.none())
        for form in teho_formset:
            form.fields['tehoalue'].queryset = tehoalue.objects.filter(user=request.user).order_by('jarj_nro')

    return render(request, 'training_form.html',
        context = {
            'page_title': 'Treenipäiväkirja | Lisää harjoitus',
            'page_header': 'LISÄÄ HARJOITUS',
            'teho_formset': teho_formset,
            'harjoitus_form': harjoitus_form,
            'max_count':max_count
            })


@login_required
def training_modify(request,pk):
    """ 
    Modifies training information 
    """
    max_count = 20
    TehoFormset = inlineformset_factory(harjoitus,teho,form=TehoForm,extra=max_count,max_num=max_count,can_delete=True)
    training = harjoitus.objects.get(id=pk,user_id=request.user.id)
    if request.method == "POST":
        harjoitus_form = HarjoitusForm(request.user,request.POST,instance=training)
        teho_formset = TehoFormset(request.POST,request.FILES,instance=training)
        if harjoitus_form.is_valid() and harjoitus_form.has_changed():
            post = harjoitus_form.save(commit=False)
            post.aika_id = post.pvm.strftime('%Y%m%d')
            kesto_h = transformations.coalesce(harjoitus_form.cleaned_data['kesto_h'],0)
            kesto_min = transformations.coalesce(harjoitus_form.cleaned_data['kesto_min'],0)
            post.kesto_h = kesto_h
            post.kesto_min = kesto_min
            post.kesto = transformations.h_min_to_hours(kesto_h,kesto_min)
            vauhti_km_h = harjoitus_form.cleaned_data['vauhti_km_h']
            vauhti_m = harjoitus_form.cleaned_data['vauhti_min']
            vauhti_s = harjoitus_form.cleaned_data['vauhti_s']
            post.vauhti_min_km = transformations.vauhti_min_km(vauhti_m,vauhti_s)
            if post.vauhti_min_km is None and vauhti_km_h is not None:
                post.vauhti_min_km = 60 / vauhti_km_h
            elif post.vauhti_min_km is not None and vauhti_km_h is None:
                post.vauhti_km_h = 60 / post.vauhti_min_km
            post.save()
        if teho_formset.is_valid() and teho_formset.has_changed():
            teho_formset.save()
        return redirect('trainings')
    else:
        if training.vauhti_min_km is None:
            vauhti_m = None
            vauhti_s = None
        else:
            vauhti_m = int(training.vauhti_min_km)
            vauhti_s = round((training.vauhti_min_km*60) % 60,0)
        harjoitus_form = HarjoitusForm(request.user,instance=training,initial={'vauhti_min': vauhti_m, 'vauhti_s': vauhti_s })
        teho_formset = TehoFormset(instance=training)
        for form in teho_formset:
            form.fields['tehoalue'].queryset = tehoalue.objects.filter(user=request.user)
    
    return render(request, 'training_form.html',
        context = {
            'page_title': 'Treenipäiväkirja | Muokkaa harjoitusta',
            'page_header': 'MUOKKAA HARJOITUSTA',
            'teho_formset': teho_formset,
            'harjoitus_form': harjoitus_form,
            'max_count':max_count
            })


@login_required
def training_delete(request,pk):
    """ 
    Deletes training 
    """
    training = harjoitus.objects.get(id=pk,user_id=request.user.id)
    day = training.pvm
    sport = training.laji
    duration = training.kesto

    if request.method == "POST":
        response = request.POST['confirm']
        if response == 'no':
            return redirect('trainings')
        if response == 'yes':
            training.delete()
            return redirect('trainings')

    return render(request,'training_delete.html',
        context = {
            'day': day,
            'sport': sport,
            'duration': duration
            })


@login_required
def settings_view(request):
    """ 
    Settings page 
    """
    current_user = request.user

    SeasonsFormset = inlineformset_factory(User, kausi, form=KausiForm, extra=1, can_delete=True)
    ZonesFormset = inlineformset_factory(User, tehoalue, form=TehoalueForm, extra=1, can_delete=True)
    SportsFormset = inlineformset_factory(User, laji, form=LajiForm, extra=1, can_delete=True)

    zones_required_fields = [f.name for f in tehoalue._meta.get_fields() if not getattr(f, 'blank', False) is True]
    seasons_required_fields = [f.name for f in kausi._meta.get_fields() if not getattr(f, 'blank', False) is True]
    sports_required_fields = [f.name for f in laji._meta.get_fields() if not getattr(f, 'blank', False) is True]

    if request.method == 'GET':
        page = request.GET.get('page','')
        if page not in ['profile','pw_reset','seasons','sports','zones']:
            page = 'profile'
    
    if request.method == 'POST':
        if 'profile_save' in request.POST:
            page = 'profile'
            user_form = UserForm(request.POST, instance=current_user)
            if user_form.is_valid():
                user_form.save()
                return redirect('settings')
            else:
                pw_form = PasswordChangeForm(user=current_user)

        if 'pw_save' in request.POST:
            page = 'pw_reset'
            pw_form = PasswordChangeForm(data=request.POST, user=current_user)
            if pw_form.is_valid():
                pw_form.save()
                update_session_auth_hash(request, pw_form.user)
                messages.add_message(request, messages.SUCCESS, 'Salasana vaihdettu.')
                return redirect('settings')
            else:
                user_form = UserForm(instance=current_user)

        if 'sports_save' in request.POST:
            page = 'sports'
            sports_formset = SportsFormset(request.POST, request.FILES, instance=current_user)
            if sports_formset.is_valid() and sports_formset.has_changed():
                try:
                    sports_formset.save()
                except ProtectedError:
                    messages.add_message(request, messages.ERROR, 'Lajia ei voida poistaa, koska siihen on liitetty harjoituksia.')

        if 'zones_save' in request.POST:
            page = 'zones'
            zones_formset = ZonesFormset(request.POST, request.FILES, instance=current_user)
            if zones_formset.is_valid() and zones_formset.has_changed():
                try:
                    zones_formset.save()
                except ProtectedError:
                    messages.add_message(request, messages.ERROR, 'Tehoaluetta ei voida poistaa, koska siihen on liitetty harjoituksia.')

        if 'seasons_save' in request.POST:
            page = 'seasons'
            seasons_formset = SeasonsFormset(request.POST, request.FILES, instance=current_user)
            if seasons_formset.is_valid() and seasons_formset.has_changed():
                seasons_formset.save()

    user_form = UserForm(instance=current_user)
    pw_form = PasswordChangeForm(user=current_user)
    seasons_formset = SeasonsFormset(instance=current_user)
    zones_formset = ZonesFormset(instance=current_user)
    sports_formset = SportsFormset(instance=current_user)

    return render(request,'settings.html',
        context = {
            'user_form': user_form,
            'pw_form': pw_form,
            'sports_formset': sports_formset,
            'sports_required_fields': sports_required_fields,
            'seasons_formset': seasons_formset,
            'seasons_required_fields': seasons_required_fields,
            'zones_formset': zones_formset,
            'zones_required_fields': zones_required_fields,
            'page': page
            })


def register(request):
    """ User registration """
    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('accounts/login')
    else:
        form = RegistrationForm() 
    return render(request, 'register.html', 
        context = {'form': form})


@login_required
def trainings_data(request):
    current_user_id = request.user.id
    table_columns = request.POST.getlist('columns[]')
    trainings_df = transformations.trainings_datatable(current_user_id)
    if trainings_df is None:
        trainings_list = []
    else:
        trainings_df = trainings_df[table_columns]
        trainings_list = trainings_df.fillna('').values.tolist()
    return JsonResponse(trainings_list, safe=False)
