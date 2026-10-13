"""Matches played by the Forge bridge: a game id, turns instead of rounds,
Forge's end reasons, a stopped status and the forge_ai seat."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('playtest', '0005_remove_benchmark_seats'),
    ]

    operations = [
        migrations.RemoveField(model_name='match', name='max_rounds'),
        migrations.RenameField(model_name='match', old_name='round', new_name='turn'),
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
        migrations.AlterField(
            model_name='match',
            name='end_reason',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AlterField(
            model_name='match',
            name='status',
            field=models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('finished', 'Finished'), ('failed', 'Failed'), ('stopped', 'Stopped')], default='pending', max_length=16),
        ),
        migrations.RemoveField(model_name='seat', name='eliminated_round'),
        migrations.AlterField(
            model_name='seat',
            name='agent',
            field=models.CharField(choices=[('forge_ai', 'Forge AI'), ('random', 'Random'), ('heuristic', 'Heuristic'), ('jev', 'Jev'), ('llm', 'LLM')], default='forge_ai', max_length=16),
        ),
        migrations.RemoveField(model_name='matchevent', name='round'),
    ]
