"""Matches played by the Forge bridge in playtest sessions: a game id, turns instead
of rounds, Forge's end reasons, a stopped status, metagame opponents, and the
forge_ai and jev seats replacing the LLM one."""

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


def llm_to_forge_ai(apps, schema_editor):
    """The LLM agent is gone; Forge's AI takes its seats, as it would at play time."""
    Seat = apps.get_model("playtest", "Seat")
    Seat.objects.filter(agent="llm").update(agent="forge_ai")


class Migration(migrations.Migration):

    dependencies = [
        ('decks', '0005_constructed_formats_and_metagame'),
        ('playtest', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveField(
            model_name='match',
            name='max_rounds',
        ),
        migrations.RenameField(
            model_name='match',
            old_name='round',
            new_name='turn',
        ),
        migrations.RemoveField(
            model_name='matchevent',
            name='round',
        ),
        migrations.RemoveField(
            model_name='seat',
            name='eliminated_round',
        ),
        migrations.AddField(
            model_name='match',
            name='forge_game_id',
            field=models.CharField(blank=True, db_index=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='match',
            name='started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='match',
            name='summary',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='matchevent',
            name='step',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='seat',
            name='coverage',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='seat',
            name='metagame_deck',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='seats', to='decks.metagamedeck'),
        ),
        migrations.AlterField(
            model_name='match',
            name='end_reason',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AlterField(
            model_name='match',
            name='format',
            field=models.CharField(choices=[('commander', 'Commander'), ('duel', 'Duel Commander'), ('standard', 'Standard'), ('pioneer', 'Pioneer'), ('modern', 'Modern'), ('legacy', 'Legacy'), ('vintage', 'Vintage'), ('pauper', 'Pauper')], default='commander', max_length=16),
        ),
        migrations.AlterField(
            model_name='match',
            name='status',
            field=models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('finished', 'Finished'), ('failed', 'Failed'), ('stopped', 'Stopped')], default='pending', max_length=16),
        ),
        migrations.RunPython(llm_to_forge_ai, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='seat',
            name='agent',
            field=models.CharField(choices=[('forge_ai', 'Forge AI'), ('random', 'Random'), ('jev', 'Jev')], default='jev', max_length=16),
        ),
        migrations.CreateModel(
            name='Playtest',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('format', models.CharField(choices=[('commander', 'Commander'), ('duel', 'Duel Commander'), ('standard', 'Standard'), ('pioneer', 'Pioneer'), ('modern', 'Modern'), ('legacy', 'Legacy'), ('vintage', 'Vintage'), ('pauper', 'Pauper')], default='commander', max_length=16)),
                ('deck_name', models.CharField(max_length=255)),
                ('seed', models.BigIntegerField()),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('deck', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='playtests', to='decks.deck')),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='playtests', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'playtests',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='match',
            name='playtest',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='matches', to='playtest.playtest'),
        ),
    ]
