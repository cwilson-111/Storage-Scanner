"""Tests for forecasting module, especially handling of None values."""

import pytest
from datetime import datetime, timedelta
from storage_scanner import forecasting


class TestFormatForecastRange:
    """Test format_forecast_range with various forecast scenarios."""

    def test_format_forecast_range_with_both_bounds(self):
        """Normal case: both optimistic and pessimistic bounds exist."""
        forecast = forecasting.Forecast(
            status="ok",
            days_estimate=100,
            days_optimistic=80,
            days_pessimistic=120,
            confidence="high",
            r_squared=0.95,
            data_points=10,
            span_days=30.0,
        )
        result = forecasting.format_forecast_range(forecast)
        assert result == "~80–120 days"

    def test_format_forecast_range_with_none_optimistic(self):
        """Noisy history where slow_slope <= 0: days_optimistic is None."""
        forecast = forecasting.Forecast(
            status="ok",
            days_estimate=266,
            days_optimistic=None,
            days_pessimistic=55,
            confidence="low",
            r_squared=0.02,
            data_points=5,
            span_days=4.0,
        )
        result = forecasting.format_forecast_range(forecast)
        assert result == "at least 55 days"

    def test_format_forecast_range_insufficient_data(self):
        """Insufficient data forecast returns None."""
        forecast = forecasting.Forecast(
            status="insufficient_data",
            days_estimate=None,
            days_optimistic=None,
            days_pessimistic=None,
            confidence=None,
            r_squared=None,
            data_points=2,
            span_days=None,
        )
        result = forecasting.format_forecast_range(forecast)
        assert result is None

    def test_format_forecast_range_not_growing(self):
        """Not growing forecast returns None."""
        forecast = forecasting.Forecast(
            status="not_growing",
            days_estimate=None,
            days_optimistic=None,
            days_pessimistic=None,
            confidence=None,
            r_squared=0.5,
            data_points=5,
            span_days=10.0,
        )
        result = forecasting.format_forecast_range(forecast)
        assert result is None

    def test_format_forecast_range_equal_bounds(self):
        """When both bounds are equal, show a point estimate."""
        forecast = forecasting.Forecast(
            status="ok",
            days_estimate=100,
            days_optimistic=100,
            days_pessimistic=100,
            confidence="medium",
            r_squared=0.85,
            data_points=8,
            span_days=20.0,
        )
        result = forecasting.format_forecast_range(forecast)
        assert result == "~100 days"

    def test_format_forecast_range_reversed_bounds(self):
        """sorted() should handle bounds in any order."""
        forecast = forecasting.Forecast(
            status="ok",
            days_estimate=100,
            days_optimistic=120,  # Note: optimistic > pessimistic
            days_pessimistic=80,
            confidence="high",
            r_squared=0.9,
            data_points=10,
            span_days=30.0,
        )
        result = forecasting.format_forecast_range(forecast)
        assert result == "~80–120 days"


class TestForecastWithNoisyHistory:
    """Integration test: forecast with the noisy history from P1-4 spec."""

    def test_noisy_history_produces_none_optimistic(self):
        """Daily sizes 100, 80, 120, 85, 105 GB should produce None days_optimistic."""
        base_date = datetime(2026, 9, 1)
        history = [
            ((base_date + timedelta(days=i)).isoformat(), size * 1e9, 1000, 100)
            for i, size in enumerate([100, 80, 120, 85, 105])
        ]

        drive_capacity = 500e9  # 500 GB
        forecast = forecasting.forecast_days_until_full(history, drive_capacity)

        assert forecast.status == "ok"
        assert forecast.days_optimistic is None
        assert forecast.days_pessimistic is not None
        assert forecast.days_estimate is not None

    def test_noisy_history_formats_correctly(self):
        """The formatted output should show 'at least N days'."""
        base_date = datetime(2026, 9, 1)
        history = [
            ((base_date + timedelta(days=i)).isoformat(), size * 1e9, 1000, 100)
            for i, size in enumerate([100, 80, 120, 85, 105])
        ]

        drive_capacity = 500e9  # 500 GB
        forecast = forecasting.forecast_days_until_full(history, drive_capacity)
        range_text = forecasting.format_forecast_range(forecast)

        # Should produce "at least N days" format
        assert range_text is not None
        assert "at least" in range_text
        assert "days" in range_text
